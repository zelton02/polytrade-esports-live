import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from polytrade_esports.engine import tick
from polytrade_esports.executor import process_due_orders, taker_fee
from polytrade_esports.paper import PaperConfig
from polytrade_esports.storage import Database
from polytrade_esports.timeutil import isoformat, parse_timestamp
from polytrade_esports.types import BookQuote, LiveState, Match


STAMP = "2026-08-27T00:00:00Z"


def depth_quote(
    stamp=STAMP,
    match_id="m1",
    ask_a=((0.50, 100.0),),
    bid_a=((0.49, 100.0),),
    ask_b=((0.52, 100.0),),
    bid_b=((0.50, 100.0),),
):
    def levels(values):
        return [{"price": str(price), "size": str(size)} for price, size in values]

    return BookQuote(
        match_id,
        bid_a=max(price for price, _ in bid_a),
        ask_a=min(price for price, _ in ask_a),
        bid_b=max(price for price, _ in bid_b),
        ask_b=min(price for price, _ in ask_b),
        source_at=stamp,
        observed_at=stamp,
        source="test-depth",
        raw={
            "A": {"bids": levels(bid_a), "asks": levels(ask_a)},
            "B": {"bids": levels(bid_b), "asks": levels(ask_b)},
        },
    ).normalized()


class ExecutionSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "execution.sqlite3"))
        self.db.initialize()
        self.db.add_match(
            Match("m1", "A", "B", 3, 0.70, token_a="ta", token_b="tb")
        )
        self.config = PaperConfig(
            latency_ms=0,
            latency_jitter_ms=0,
            order_ttl_seconds=20,
            max_market_participation=1.0,
        )

    def tearDown(self):
        self.temp.cleanup()

    def signal(self, quote=None, stamp=STAMP, **kwargs):
        quote = quote or depth_quote(stamp=stamp)
        return tick(
            self.db,
            LiveState("m1", stamp, 0, 0, 0, 0, observed_at=stamp),
            quote,
            paper_config=kwargs.pop("config", self.config),
            decision_at=stamp,
            **kwargs,
        )

    def execute(self, quote=None, stamp=STAMP):
        quote = quote or depth_quote(stamp=stamp)
        return process_due_orders(
            self.db,
            "live-paper",
            now=stamp,
            supplied_quotes={"m1": quote},
        )

    def test_signal_creates_an_order_but_never_an_instant_trade(self):
        result = self.signal()
        self.assertEqual(result["paper_actions"][0]["status"], "PENDING")
        payload = self.db.dashboard_payload()
        self.assertEqual(payload["counts"]["orders"], 1)
        self.assertEqual(payload["counts"]["fills"], 0)
        self.assertEqual(payload["counts"]["trades"], 0)
        self.assertEqual(self.db.account_payload()["positions"], [])

    def test_default_risk_cap_order_is_not_a_rounding_partial(self):
        self.signal()
        batch = self.execute()
        self.assertEqual((batch.filled, batch.partial), (1, 0))
        self.assertAlmostEqual(batch.orders[0]["fill_rate"], 1.0)

    def test_repeating_the_same_forecast_is_idempotent(self):
        first = self.signal()
        second = self.signal()
        self.assertEqual(first["forecast_id"], second["forecast_id"])
        self.assertEqual(
            first["paper_actions"][0]["order_id"],
            second["paper_actions"][0]["order_id"],
        )
        with self.db.connect() as connection:
            orders = connection.execute(
                "SELECT count(*) AS n, min(status) AS status FROM paper_orders"
            ).fetchone()
        self.assertEqual(orders["n"], 1)
        self.assertEqual(orders["status"], "PENDING")

    def test_only_an_execution_snapshot_normalizes_depth_rows(self):
        quote = depth_quote()
        self.signal(quote)
        with self.db.connect() as connection:
            before = connection.execute(
                "SELECT count(*) FROM order_book_levels"
            ).fetchone()[0]
        self.assertEqual(before, 0)
        self.execute(quote)
        with self.db.connect() as connection:
            after = connection.execute(
                "SELECT count(*) FROM order_book_levels"
            ).fetchone()[0]
            depth_books = connection.execute(
                "SELECT count(*) FROM market_snapshots WHERE depth_available=1"
            ).fetchone()[0]
        self.assertEqual(after, 4)
        self.assertEqual(depth_books, 1)

    def test_walks_depth_and_charges_the_sports_taker_curve(self):
        config = PaperConfig(
            max_match_fraction=0.02,
            kelly_scale=0.025,
            latency_ms=0,
            latency_jitter_ms=0,
            order_ttl_seconds=20,
            max_market_participation=1.0,
        )
        quote = depth_quote(ask_a=((0.50, 5.0), (0.52, 100.0)))
        signalled = self.signal(quote, config=config)
        requested = signalled["paper_actions"][0]["shares"]
        batch = self.execute(quote)
        self.assertEqual((batch.filled, batch.partial, batch.rejected), (1, 0, 0))
        order = batch.orders[0]
        self.assertAlmostEqual(order["filled_shares"], requested, places=6)
        expected_average = (5.0 * 0.50 + (requested - 5.0) * 0.52) / requested
        self.assertAlmostEqual(order["avg_fill_price"], expected_average, places=6)
        expected_fee = (
            taker_fee(5.0, 0.50, 0.03)
            + taker_fee(requested - 5.0, 0.52, 0.03)
        )
        self.assertAlmostEqual(order["fee"], expected_fee, places=6)
        account = self.db.account_payload()
        self.assertEqual(len(account["positions"]), 1)
        self.assertGreater(account["positions"][0]["avg_cost"], expected_average)
        self.assertEqual(account["trades"][0]["execution_mode"], "depth-sim")

    def test_market_participation_produces_an_audited_partial_fill(self):
        config = PaperConfig(
            latency_ms=0,
            latency_jitter_ms=0,
            order_ttl_seconds=20,
            max_market_participation=0.10,
        )
        quote = depth_quote(ask_a=((0.50, 10.0),))
        self.signal(quote, config=config)
        batch = self.execute(quote)
        self.assertEqual(batch.partial, 1)
        self.assertAlmostEqual(batch.orders[0]["filled_shares"], 1.0)
        self.assertEqual(batch.orders[0]["reason"], "insufficient_executable_depth")
        summary = self.db.execution_summary()
        self.assertAlmostEqual(
            summary["fill_rate"],
            1.0 / batch.orders[0]["requested_shares"],
        )

    def test_adverse_price_move_reports_risk_budget_not_missing_depth(self):
        signal_book = depth_quote(ask_a=((0.50, 100.0),))
        self.signal(signal_book)
        execution_book = depth_quote(ask_a=((0.53, 100.0),))
        batch = self.execute(execution_book)
        self.assertEqual(batch.partial, 1)
        self.assertEqual(batch.orders[0]["reason"], "risk_budget_partial")
        self.assertAlmostEqual(batch.orders[0]["avg_fill_price"], 0.53)

    def test_stale_book_is_rejected_without_moving_cash(self):
        self.signal()
        later = isoformat(parse_timestamp(STAMP) + timedelta(seconds=6))
        batch = self.execute(depth_quote(), stamp=later)
        self.assertEqual(batch.rejected, 1)
        self.assertEqual(batch.orders[0]["reason"], "stale_book")
        self.assertEqual(self.db.account_payload()["cash"], 1000.0)

    def test_kill_switch_blocks_entries_but_not_risk_reducing_exits(self):
        opening = depth_quote()
        self.signal(opening)
        self.execute(opening)

        self.db.set_execution_kill_switch("live-paper", True, "operator")
        later = isoformat(parse_timestamp(STAMP) + timedelta(seconds=1))
        exit_book = depth_quote(
            stamp=later,
            bid_a=((0.75, 100.0),),
            ask_a=((0.76, 100.0),),
            bid_b=((0.23, 100.0),),
            ask_b=((0.24, 100.0),),
        )
        result = self.signal(
            exit_book,
            stamp=later,
            entry_enabled=False,
        )
        self.assertEqual(result["paper_actions"][0]["action"], "SELL")
        batch = self.execute(exit_book, stamp=later)
        self.assertEqual(batch.filled, 1)
        self.assertEqual(self.db.account_payload()["positions"], [])

    def test_missing_depth_fails_closed(self):
        quote = depth_quote()
        self.signal(quote)
        bbo_only = BookQuote(
            "m1", 0.49, 0.50, 0.50, 0.52, STAMP,
            observed_at=STAMP, source="bbo-only",
        )
        batch = self.execute(bbo_only)
        self.assertEqual(batch.rejected, 1)
        self.assertEqual(batch.orders[0]["reason"], "missing_depth")

    def test_max_open_positions_is_enforced_at_fill_time(self):
        config = PaperConfig(
            latency_ms=0,
            latency_jitter_ms=0,
            order_ttl_seconds=20,
            max_market_participation=1.0,
            max_open_positions=1,
        )
        self.signal(config=config)
        self.execute()

        later = isoformat(parse_timestamp(STAMP) + timedelta(seconds=1))
        self.db.add_match(Match("m2", "C", "D", 3, 0.70))
        quote = depth_quote(stamp=later, match_id="m2")
        tick(
            self.db,
            LiveState("m2", later, 0, 0, 0, 0, observed_at=later),
            quote,
            paper_config=config,
            decision_at=later,
        )
        batch = process_due_orders(
            self.db,
            "live-paper",
            now=later,
            supplied_quotes={"m2": quote},
        )
        self.assertEqual(batch.rejected, 1)
        self.assertEqual(batch.orders[0]["reason"], "max_open_positions")

    def test_total_exposure_ceiling_is_enforced_at_fill_time(self):
        config = PaperConfig(
            latency_ms=0,
            latency_jitter_ms=0,
            order_ttl_seconds=20,
            max_market_participation=1.0,
            max_total_exposure_fraction=0.02,
        )
        self.signal(config=config)
        self.execute()

        later = isoformat(parse_timestamp(STAMP) + timedelta(seconds=1))
        self.db.add_match(Match("m2", "C", "D", 3, 0.70))
        quote = depth_quote(stamp=later, match_id="m2")
        signalled = tick(
            self.db,
            LiveState("m2", later, 0, 0, 0, 0, observed_at=later),
            quote,
            paper_config=config,
            decision_at=later,
        )
        self.assertTrue(signalled["paper_actions"])
        # Simulate exposure changing after planning but before execution. The
        # strategy precheck reduces obvious noise; the executor remains the
        # authoritative gate for races and stale planner views.
        with self.db.connect() as connection:
            position = connection.execute(
                "SELECT shares FROM paper_positions WHERE match_id='m1'"
            ).fetchone()
            connection.execute(
                "UPDATE paper_positions SET avg_cost=? WHERE match_id='m1'",
                (20.0 / float(position["shares"]),),
            )
        batch = process_due_orders(
            self.db,
            "live-paper",
            now=later,
            supplied_quotes={"m2": quote},
        )
        self.assertEqual(batch.rejected, 1)
        self.assertEqual(batch.orders[0]["reason"], "risk_budget_exhausted")

    def test_daily_realized_loss_limit_blocks_a_new_entry(self):
        signalled = self.signal()
        account_id = self.db.ensure_account("live-paper")
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_trades(
                    account_id, match_id, forecast_id, action, outcome, shares,
                    price, cash_delta, realized_pnl, reason, traded_at,
                    execution_mode
                ) VALUES(?, 'm1', ?, 'SELL', 'A', 1, 0.5, 0, -30,
                         'test_loss', ?, 'depth-sim')
                """,
                (account_id, signalled["forecast_id"], STAMP),
            )
        batch = self.execute()
        self.assertEqual(batch.rejected, 1)
        self.assertEqual(batch.orders[0]["reason"], "daily_loss_limit")

    def test_market_data_errors_retry_then_fail_closed(self):
        class BrokenBooks:
            def get_pair(self, *_args):
                raise OSError("venue unavailable")

        config = PaperConfig(
            latency_ms=0,
            latency_jitter_ms=0,
            order_ttl_seconds=20,
            max_attempts=2,
        )
        self.signal(config=config)
        first = process_due_orders(
            self.db, "live-paper", book_client=BrokenBooks(), now=STAMP
        )
        self.assertEqual(first.retried, 1)
        later = isoformat(parse_timestamp(STAMP) + timedelta(seconds=1))
        second = process_due_orders(
            self.db, "live-paper", book_client=BrokenBooks(), now=later
        )
        self.assertEqual(second.rejected, 1)
        with self.db.connect() as connection:
            order = connection.execute("SELECT * FROM paper_orders").fetchone()
        self.assertEqual(order["status"], "REJECTED")
        self.assertEqual(order["rejection_reason"], "market_data_unavailable")
        self.assertEqual(order["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
