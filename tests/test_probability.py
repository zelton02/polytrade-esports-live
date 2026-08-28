import unittest

from polytrade_esports.probability import live_probability
from polytrade_esports.types import LiveState, Match


STAMP = "2026-08-27T00:00:00Z"


def state(**overrides):
    values = {
        "match_id": "m1",
        "source_at": STAMP,
        "observed_at": STAMP,
        "maps_a": 1,
        "maps_b": 1,
        "rounds_a": 0,
        "rounds_b": 0,
    }
    values.update(overrides)
    return LiveState(**values)


class ProbabilityTests(unittest.TestCase):
    def setUp(self):
        self.match = Match("m1", "A", "B", 3, 0.5)

    def test_even_decider_starts_even(self):
        result = live_probability(self.match, state())
        self.assertAlmostEqual(result.live_series_probability_a, 0.5, places=4)

    def test_round_lead_is_monotonic(self):
        trailing = live_probability(self.match, state(rounds_a=5, rounds_b=8))
        leading = live_probability(self.match, state(rounds_a=8, rounds_b=5))
        self.assertLess(trailing.live_series_probability_a, 0.5)
        self.assertGreater(leading.live_series_probability_a, 0.5)
        self.assertGreater(leading.live_series_probability_a, trailing.live_series_probability_a)

    def test_economy_advantage_increases_probability(self):
        weak = live_probability(self.match, state(rounds_a=6, rounds_b=6, economy_a=-1, economy_b=1))
        strong = live_probability(self.match, state(rounds_a=6, rounds_b=6, economy_a=1, economy_b=-1))
        self.assertGreater(strong.live_series_probability_a, weak.live_series_probability_a)

    def test_completed_map_is_effectively_certain(self):
        result = live_probability(self.match, state(rounds_a=13, rounds_b=6))
        self.assertGreater(result.live_series_probability_a, 0.999)


if __name__ == "__main__":
    unittest.main()

