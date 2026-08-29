import tempfile
import unittest
from pathlib import Path

from polytrade_esports.engine import tick
from polytrade_esports.storage import Database
from polytrade_esports.timeutil import isoformat, utc_now
from polytrade_esports.types import BookQuote, LiveState, Match


STAMP = "2026-08-27T00:00:00Z"


class EnginePaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "paper.sqlite3"))
        self.db.initialize()
        self.db.add_match(Match("m1", "A", "B", 3, 0.70))

    def tearDown(self):
        self.temp.cleanup()

    def test_entry_is_capped_at_one_percent(self):
        state = LiveState("m1", STAMP, 0, 0, 0, 0, observed_at=STAMP)
        quote = BookQuote("m1", 0.48, 0.50, 0.50, 0.52, STAMP, observed_at=STAMP)
        result = tick(self.db, state, quote)
        self.assertEqual(result["paper_actions"][0]["action"], "BUY")
        account = self.db.account_payload()
        spent = 1000.0 - account["cash"]
        self.assertLessEqual(spent, 10.0 + 1e-8)

    def test_strategy_is_written_to_forecast_trade_position_and_summary(self):
        state = LiveState("m1", STAMP, 0, 0, 4, 2, observed_at=STAMP)
        quote = BookQuote("m1", 0.48, 0.50, 0.50, 0.52, STAMP, observed_at=STAMP)
        result = tick(self.db, state, quote, strategy="round-live")
        self.assertEqual(result["strategy"], "round-live")
        account = self.db.account_payload()
        self.assertEqual(account["trades"][0]["decision_strategy"], "round-live")
        self.assertEqual(account["trades"][0]["entry_strategy"], "round-live")
        self.assertEqual(account["positions"][0]["entry_strategy"], "round-live")
        cohort = next(
            item for item in account["strategies"] if item["strategy"] == "round-live"
        )
        self.assertEqual(cohort["decisions"], 1)
        self.assertEqual(cohort["trades"], 1)
        self.assertEqual(cohort["open_positions"], 1)

    def test_no_entry_without_required_edge(self):
        self.db.add_match(Match("m2", "C", "D", 3, 0.50))
        state = LiveState("m2", STAMP, 0, 0, 0, 0, observed_at=STAMP)
        quote = BookQuote("m2", 0.49, 0.51, 0.49, 0.51, STAMP, observed_at=STAMP)
        result = tick(self.db, state, quote)
        self.assertEqual(result["paper_actions"], [])

    def test_disabled_entry_still_allows_risk_reducing_exit(self):
        state = LiveState("m1", STAMP, 0, 0, 0, 0, observed_at=STAMP)
        opening = BookQuote("m1", 0.48, 0.50, 0.50, 0.52, STAMP, observed_at=STAMP)
        self.assertTrue(tick(self.db, state, opening)["paper_actions"])

        later = "2026-08-27T00:01:00Z"
        expensive = BookQuote(
            "m1", 0.75, 0.76, 0.23, 0.24, later, observed_at=later
        )
        result = tick(
            self.db,
            LiveState("m1", later, 0, 0, 0, 0, observed_at=later),
            expensive,
            entry_enabled=False,
        )
        self.assertFalse(result["entry_enabled"])
        self.assertTrue(
            any(action["action"] == "SELL" for action in result["paper_actions"])
        )

    def test_state_change_can_close_or_flip_position(self):
        opening = LiveState("m1", STAMP, 0, 0, 0, 0, observed_at=STAMP)
        first_book = BookQuote("m1", 0.48, 0.50, 0.50, 0.52, STAMP, observed_at=STAMP)
        tick(self.db, opening, first_book)
        later_stamp = "2026-08-27T00:10:00Z"
        trailing = LiveState("m1", later_stamp, 1, 1, 4, 11, economy_a=-1, economy_b=1, observed_at=later_stamp)
        later_book = BookQuote("m1", 0.12, 0.14, 0.86, 0.88, later_stamp, observed_at=later_stamp)
        result = tick(self.db, trailing, later_book)
        actions = [item["action"] for item in result["paper_actions"]]
        self.assertIn("SELL", actions)
        account = self.db.account_payload()
        open_cost = sum(item["shares"] * item["avg_cost"] for item in account["positions"])
        # Realized losses consume the same $10 match-risk budget.
        self.assertLessEqual(open_cost - account["realized_pnl"], 10.0 + 1e-8)

    def test_price_drop_does_not_average_beyond_risk_cap(self):
        opening = LiveState("m1", STAMP, 0, 0, 0, 0, observed_at=STAMP)
        first_book = BookQuote("m1", 0.48, 0.50, 0.50, 0.52, STAMP, observed_at=STAMP)
        tick(self.db, opening, first_book)
        later_stamp = "2026-08-27T00:05:00Z"
        stronger = LiveState("m1", later_stamp, 1, 1, 8, 5, economy_a=1, economy_b=-1, observed_at=later_stamp)
        cheaper_book = BookQuote("m1", 0.28, 0.30, 0.70, 0.72, later_stamp, observed_at=later_stamp)
        tick(self.db, stronger, cheaper_book)
        account = self.db.account_payload()
        open_cost = sum(item["shares"] * item["avg_cost"] for item in account["positions"])
        self.assertLessEqual(open_cost, 10.0 + 1e-8)


if __name__ == "__main__":
    unittest.main()


class StaleModelGuardTests(unittest.TestCase):
    """Entry must not open on an edge created purely by market movement.

    Reconstructed from a real sequence: the model sat at 0.210 for five hours
    at 0-0 while the market ran 0.205 -> 0.425 -> 0.085. The 0.085 was map one
    ending. The engine read it as a 12-point edge, bought at 0.09, and sold
    flat a minute later once the state feed finally caught up.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "d.sqlite3"))
        self.db.initialize()
        self.db.ensure_account("live-paper", 1000.0)
        self.db.add_match(Match("m1", "M80", "FURIA", 3, 0.21))

    def tearDown(self):
        self.temp.cleanup()

    def _tick(self, ask_a, bid_a, maps=(0, 0)):
        now = isoformat(utc_now())
        state = LiveState("m1", now, maps[0], maps[1], 0, 0, observed_at=now).normalized()
        quote = BookQuote(
            "m1", bid_a, ask_a, 1 - ask_a - 0.01, 1 - bid_a, now, now, "test"
        ).normalized()
        return tick(self.db, state, quote, account_name="live-paper")

    def test_an_edge_from_market_movement_alone_does_not_open_a_position(self):
        self._tick(ask_a=0.21, bid_a=0.20)
        result = self._tick(ask_a=0.09, bid_a=0.08)
        self.assertGreater(result["edge_a"], 0.10, "the raw edge should look tradable")
        self.assertLess(result["market_drift"], -0.08)
        self.assertEqual(result["paper_actions"], [], "blindness is not an edge")

    def test_a_state_change_re_anchors_and_lets_the_edge_trade(self):
        self._tick(ask_a=0.21, bid_a=0.20)
        self.assertEqual(self._tick(ask_a=0.09, bid_a=0.08)["paper_actions"], [])
        moved = self._tick(ask_a=0.09, bid_a=0.08, maps=(1, 0))
        self.assertAlmostEqual(moved["market_drift"], 0.0, places=6)
        self.assertTrue(
            any(a["action"] == "BUY" for a in moved["paper_actions"]),
            "an edge our own feed has seen should trade",
        )

    def test_the_guard_never_blocks_an_exit(self):
        self._tick(ask_a=0.21, bid_a=0.20)
        self._tick(ask_a=0.09, bid_a=0.08)
        opened = self._tick(ask_a=0.09, bid_a=0.08, maps=(1, 0))
        self.assertTrue(any(a["action"] == "BUY" for a in opened["paper_actions"]))
        # The market now runs far away with no state change of our own.
        closed = self._tick(ask_a=0.95, bid_a=0.94, maps=(1, 0))
        self.assertGreater(abs(closed["market_drift"]), 0.08)
        self.assertTrue(
            any(a["action"] == "SELL" for a in closed["paper_actions"]),
            "being blind is a reason to stop buying, never to hold on",
        )

    def test_the_anchor_survives_repeated_ticks_of_an_unchanged_state(self):
        # Each tick writes a new state row because source_at moves, so keying
        # the anchor on state_id made the drift permanently zero and the guard
        # inert. The anchor has to follow the state's content.
        self._tick(ask_a=0.21, bid_a=0.20)
        for _ in range(4):
            result = self._tick(ask_a=0.20, bid_a=0.19)
        self.assertAlmostEqual(result["market_drift"], -0.010, places=3)
