import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from polytrade_esports.storage import Database
from polytrade_esports.timeutil import isoformat, utc_now
from polytrade_esports.types import BookQuote, LiveState, Match


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "test.sqlite3"))
        self.db.initialize()
        self.db.add_match(Match("m1", "A", "B", 3, 0.6))

    def tearDown(self):
        self.temp.cleanup()

    def test_state_is_idempotent(self):
        stamp = "2026-08-27T00:00:00Z"
        snapshot = LiveState("m1", stamp, 0, 0, 2, 1, observed_at=stamp)
        first = self.db.record_state(snapshot)
        second = self.db.record_state(snapshot)
        self.assertEqual(first, second)

    def test_frozen_prior_cannot_be_rewritten(self):
        with self.assertRaises(ValueError):
            self.db.add_match(Match("m1", "A", "B", 3, 0.7))

    def test_future_observation_is_rejected(self):
        future = isoformat(utc_now() + timedelta(minutes=1))
        with self.assertRaises(ValueError):
            LiveState("m1", future, 0, 0, 0, 0, observed_at=future).normalized()

    def test_rejected_transition_is_auditable(self):
        old = LiveState(
            "m1", "2026-08-27T00:00:00Z", 0, 0, 9, 4,
            observed_at="2026-08-27T00:00:00Z", source="sports",
        )
        new = LiveState(
            "m1", "2026-08-27T00:00:05Z", 0, 0, 8, 5,
            observed_at="2026-08-27T00:00:05Z", source="sports",
        )
        repeated = LiveState(
            "m1", "2026-08-27T00:01:05Z", 0, 0, 8, 5,
            observed_at="2026-08-27T00:01:05Z", source="sports",
        )
        first = self.db.record_state_rejection(old, new, "same_map_round_score_regressed")
        second = self.db.record_state_rejection(
            old, repeated, "same_map_round_score_regressed"
        )
        report = self.db.state_rejection_summary()
        self.assertEqual(first, second)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["recent"][0]["reason"], "same_map_round_score_regressed")

    def test_execution_schema_is_versioned_and_migratable(self):
        with self.db.connect() as connection:
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            forecast_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(forecasts)")
            }
        self.assertEqual(version, "8")
        self.assertTrue(
            {"paper_orders", "paper_fills", "order_book_levels",
             "execution_controls", "executor_status"}.issubset(tables)
        )
        self.assertIn("execution_mode", forecast_columns)

    def test_maps_only_backfill_preserves_real_boundary_and_splits_repeats(self):
        def forecast(stamp, state, strategy, execution_mode="depth-sim"):
            state_id = self.db.record_state(state)
            book_id = self.db.record_book(
                BookQuote(
                    "m1", 0.45, 0.46, 0.53, 0.54,
                    source_at=stamp, observed_at=stamp,
                )
            )
            return self.db.record_forecast(
                match_id="m1",
                state_id=state_id,
                book_id=book_id,
                forecast_at=stamp,
                model_version="migration-test",
                probability_a=0.6,
                market_midpoint_a=0.455,
                edge_a=0.14,
                edge_b=-0.14,
                best_side="A",
                breakdown={},
                strategy=strategy,
                paper_enabled=True,
                entry_enabled=strategy != "map-boundary",
                execution_mode=execution_mode,
            )

        first = "2026-08-27T00:00:00Z"
        boundary = "2026-08-27T00:01:00Z"
        repeated = "2026-08-27T00:02:00Z"
        legacy = "2026-08-27T00:03:00Z"
        forecast(
            first,
            LiveState(
                "m1", first, 0, 0, 12, 8, current_map="Map 1",
                source="polymarket-sports-ws", observed_at=first,
            ),
            "round-live",
        )
        forecast(
            boundary,
            LiveState(
                "m1", boundary, 1, 0, 0, 0, current_map="Map 2",
                source="polymarket-sports-ws-maps", observed_at=boundary,
            ),
            "map-boundary",
        )
        forecast(
            repeated,
            LiveState(
                "m1", repeated, 1, 0, 0, 0, current_map="Map 2",
                source="polymarket-sports-ws-maps", observed_at=repeated,
            ),
            "map-boundary",
        )
        forecast(
            legacy,
            LiveState(
                "m1", legacy, 1, 0, 0, 0, current_map="Map 2",
                source="polymarket-sports-ws-maps", observed_at=legacy,
            ),
            "map-boundary",
            execution_mode="legacy",
        )
        with self.db.connect() as connection:
            connection.execute(
                "DELETE FROM metadata WHERE key='backfill_maps_only_degraded_v1'"
            )

        self.db.initialize()

        with self.db.connect() as connection:
            strategies = [
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT strategy, execution_mode FROM forecasts "
                    "ORDER BY forecast_at"
                ).fetchall()
            ]
        self.assertEqual(
            strategies,
            [
                ("round-live", "depth-sim"),
                ("map-boundary", "depth-sim"),
                ("maps-only-degraded", "depth-sim"),
                ("map-boundary", "legacy"),
            ],
        )
        self.assertEqual(
            sum(item["forecasts"] for item in self.db.strategy_summary()),
            3,
        )

    def test_maps_only_backfill_does_not_guess_first_degraded_forecast(self):
        stamp = "2026-08-27T00:00:00Z"
        state_id = self.db.record_state(
            LiveState(
                "m1", stamp, 1, 0, 0, 0, current_map="Map 2",
                source="polymarket-sports-ws-maps", observed_at=stamp,
            )
        )
        book_id = self.db.record_book(
            BookQuote(
                "m1", 0.45, 0.46, 0.53, 0.54,
                source_at=stamp, observed_at=stamp,
            )
        )
        forecast_id = self.db.record_forecast(
            match_id="m1",
            state_id=state_id,
            book_id=book_id,
            forecast_at=stamp,
            model_version="migration-test",
            probability_a=0.6,
            market_midpoint_a=0.455,
            edge_a=0.14,
            edge_b=-0.14,
            best_side="A",
            breakdown={},
            strategy="map-boundary",
            paper_enabled=True,
            entry_enabled=False,
            execution_mode="depth-sim",
        )
        with self.db.connect() as connection:
            connection.execute(
                "DELETE FROM metadata WHERE key='backfill_maps_only_degraded_v1'"
            )

        self.db.initialize()

        with self.db.connect() as connection:
            strategy = connection.execute(
                "SELECT strategy FROM forecasts WHERE forecast_id=?",
                (forecast_id,),
            ).fetchone()[0]
        self.assertEqual(strategy, "map-boundary")


if __name__ == "__main__":
    unittest.main()
