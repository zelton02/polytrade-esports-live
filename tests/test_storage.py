import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from polytrade_esports.storage import Database
from polytrade_esports.timeutil import isoformat, utc_now
from polytrade_esports.types import LiveState, Match


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


if __name__ == "__main__":
    unittest.main()

