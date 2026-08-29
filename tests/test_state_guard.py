import unittest

from polytrade_esports.state_guard import (
    FROZEN_SOURCE,
    canonicalize_state,
    strategy_for_state,
    validate_strategy,
)
from polytrade_esports.types import LiveState


def state(stamp, maps=(0, 0), rounds=(0, 0), current_map="Map 1", source="sports"):
    return LiveState(
        "m1",
        stamp,
        maps[0],
        maps[1],
        rounds[0],
        rounds[1],
        current_map=current_map,
        source=source,
        observed_at=stamp,
    ).normalized()


class CanonicalStateGuardTests(unittest.TestCase):
    def test_same_map_rounds_cannot_move_backwards(self):
        previous = state("2026-08-29T00:00:00Z", rounds=(9, 4))
        candidate = state("2026-08-29T00:00:05Z", rounds=(8, 5))
        decision = canonicalize_state(previous, candidate, True)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "same_map_round_score_regressed")
        self.assertEqual((decision.state.rounds_a, decision.state.rounds_b), (9, 4))
        self.assertEqual(decision.state.source, FROZEN_SOURCE)

    def test_map_score_cannot_move_backwards(self):
        previous = state("2026-08-29T00:00:00Z", maps=(1, 0), current_map="Map 2")
        candidate = state("2026-08-29T00:00:05Z", maps=(0, 0), current_map="Map 1")
        decision = canonicalize_state(previous, candidate, True)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "map_score_regressed")

    def test_new_map_allows_round_reset(self):
        previous = state(
            "2026-08-29T00:00:00Z", maps=(0, 0), rounds=(13, 8), current_map="Map 1"
        )
        candidate = state(
            "2026-08-29T00:00:05Z", maps=(1, 0), rounds=(0, 0), current_map="Map 2"
        )
        decision = canonicalize_state(previous, candidate, True)
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.frozen)
        self.assertEqual((decision.state.maps_a, decision.state.rounds_a), (1, 0))

    def test_maps_only_same_map_freezes_rounds_without_calling_it_corruption(self):
        previous = state("2026-08-29T00:00:00Z", rounds=(9, 4))
        candidate = state(
            "2026-08-29T00:00:05Z",
            rounds=(0, 0),
            source="polymarket-sports-ws-maps",
        )
        decision = canonicalize_state(previous, candidate, False)
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.frozen)
        self.assertEqual((decision.state.rounds_a, decision.state.rounds_b), (9, 4))
        self.assertEqual(decision.reason, "round_detail_unavailable")

    def test_strategy_horizons_are_mutually_exclusive(self):
        first = state("2026-08-29T00:00:00Z")
        next_map = state(
            "2026-08-29T00:01:00Z", maps=(1, 0), current_map="Map 2"
        )
        self.assertEqual(strategy_for_state(False, None, first, False), "pre-match")
        self.assertEqual(strategy_for_state(True, first, next_map, True), "map-boundary")
        self.assertEqual(strategy_for_state(True, first, first, True), "round-live")
        self.assertEqual(
            strategy_for_state(True, first, first, False),
            "maps-only-degraded",
        )

    def test_true_map_boundary_wins_over_maps_only_degradation(self):
        first = state("2026-08-29T00:00:00Z")
        next_map = state(
            "2026-08-29T00:01:00Z", maps=(1, 0), current_map="Map 2",
            source="polymarket-sports-ws-maps",
        )
        self.assertEqual(
            strategy_for_state(True, first, next_map, False),
            "map-boundary",
        )

    def test_degraded_strategy_is_validated_but_unknown_labels_are_not(self):
        self.assertEqual(
            validate_strategy("MAPS-ONLY-DEGRADED"),
            "maps-only-degraded",
        )
        with self.assertRaises(ValueError):
            validate_strategy("live-ish")


if __name__ == "__main__":
    unittest.main()
