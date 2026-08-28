import json
import unittest
from pathlib import Path

from polytrade_esports.pandascore import build_state, team_a_index

# Captured from a real free-tier /csgo/matches/running response.
REAL = json.loads(
    (Path(__file__).parent / "fixtures" / "pandascore_running_match.json").read_text()
)

MATCH = {
    "id": 1650531,
    "status": "running",
    "number_of_games": 3,
    "modified_at": "2026-08-28T09:00:00Z",
    "opponents": [
        {"opponent": {"id": 11, "name": "Elusive"}},
        {"opponent": {"id": 22, "name": "Zomblers"}},
    ],
    "results": [{"team_id": 11, "score": 1}, {"team_id": 22, "score": 0}],
    "games": [
        {"id": 900, "position": 1, "status": "finished", "winner": {"id": 11}},
        {
            "id": 901,
            "position": 2,
            "status": "running",
            "map": {"name": "Mirage"},
            "teams": [
                {"team_id": 11, "score": 9},
                {"team_id": 22, "score": 6},
            ],
        },
    ],
}


class PandaScoreStateTests(unittest.TestCase):
    def test_maps_and_rounds_map_to_the_polymarket_a_side(self):
        state = build_state("m1", MATCH, team_a_index=0)
        self.assertEqual((state.maps_a, state.maps_b), (1, 0))
        self.assertEqual((state.rounds_a, state.rounds_b), (9, 6))
        self.assertEqual(state.current_map, "Mirage")
        self.assertEqual(state.source, "pandascore")

    def test_sides_swap_when_polymarket_lists_the_other_team_first(self):
        state = build_state("m1", MATCH, team_a_index=1)
        self.assertEqual((state.maps_a, state.maps_b), (0, 1))
        self.assertEqual((state.rounds_a, state.rounds_b), (6, 9))

    def test_rounds_fall_back_to_counting_the_rounds_array(self):
        match = dict(MATCH)
        match["games"] = [
            {
                "id": 901,
                "status": "running",
                "map": {"name": "Nuke"},
                "rounds": [
                    {"winner_team": 11},
                    {"winner_team": 22},
                    {"winner_team": 11},
                ],
            }
        ]
        state = build_state("m1", match, team_a_index=0)
        self.assertEqual((state.rounds_a, state.rounds_b), (2, 1))

    def test_missing_game_detail_still_yields_maps_only_state(self):
        match = dict(MATCH)
        match["games"] = [{"id": 900, "status": "finished", "winner": {"id": 11}}]
        state = build_state("m1", match, team_a_index=0)
        self.assertEqual((state.maps_a, state.maps_b), (1, 0))
        self.assertEqual((state.rounds_a, state.rounds_b), (0, 0))
        self.assertEqual(state.current_map, "unknown")

    def test_future_provider_timestamp_is_clamped_not_rejected(self):
        match = dict(MATCH)
        match["modified_at"] = "2099-01-01T00:00:00Z"
        state = build_state("m1", match, team_a_index=0)
        self.assertLessEqual(state.source_at, state.observed_at)

    def test_unidentifiable_opponents_return_none(self):
        self.assertIsNone(build_state("m1", {"opponents": []}, team_a_index=0))

    def test_team_a_index_matches_by_name(self):
        self.assertEqual(team_a_index(MATCH, "Zomblers"), 1)
        self.assertEqual(team_a_index(MATCH, "Elusive"), 0)

    def test_team_a_index_tolerates_cosmetic_prefixes(self):
        match = {
            "opponents": [
                {"opponent": {"id": 1, "name": "MIBR Academy"}},
                {"opponent": {"id": 2, "name": "ODDIK"}},
            ]
        }
        self.assertEqual(team_a_index(match, "ex-MIBR Academy"), 0)

    def test_free_tier_payload_yields_maps_and_map_position(self):
        # The free plan exposes results and the game list but refuses
        # /csgo/games/{id}, so there is no map name and no round score.
        state = build_state("m1", REAL, team_a_index=0)
        self.assertEqual((state.maps_a, state.maps_b), (0, 1))
        self.assertEqual((state.rounds_a, state.rounds_b), (0, 0))
        self.assertEqual(state.current_map, "MAP 2")

    def test_map_name_wins_over_position_when_the_plan_provides_it(self):
        match = dict(MATCH)
        match["games"] = [
            {"id": 9, "position": 2, "status": "running", "map": {"name": "Nuke"},
             "teams": [{"team_id": 11, "score": 4}, {"team_id": 22, "score": 1}]}
        ]
        self.assertEqual(build_state("m1", match, 0).current_map, "Nuke")

    def test_game_detail_merges_over_the_listed_game(self):
        detail = {
            "id": 226018,
            "map": {"name": "Ancient"},
            "teams": [{"team_id": 138974, "score": 7}, {"team_id": 131010, "score": 5}],
        }
        state = build_state("m1", REAL, team_a_index=0, game_detail=detail)
        self.assertEqual(state.current_map, "Ancient")
        self.assertEqual((state.rounds_a, state.rounds_b), (5, 7))
        self.assertEqual((state.maps_a, state.maps_b), (0, 1))

    def test_ct_side_nudge_is_bounded_and_signed(self):
        match = dict(MATCH)
        match["games"] = [
            {
                "id": 901,
                "status": "running",
                "map": {"name": "Ancient"},
                "teams": [{"team_id": 11, "score": 3}, {"team_id": 22, "score": 2}],
                "rounds": [{"ct_team": 11}],
            }
        ]
        self.assertGreater(build_state("m1", match, 0).side_advantage_a, 0)
        self.assertLess(build_state("m1", match, 1).side_advantage_a, 0)


if __name__ == "__main__":
    unittest.main()
