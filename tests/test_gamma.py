import copy
import json
import unittest
from pathlib import Path

from polytrade_esports.gamma import (
    best_of,
    map_score_from_markets,
    parse_event,
    series_market,
    to_match,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gamma_cs2_event.json").read_text()
)


class GammaParsingTests(unittest.TestCase):
    def setUp(self):
        self.event = copy.deepcopy(FIXTURE)

    def test_series_market_is_the_moneyline_not_a_map(self):
        market = series_market(self.event)
        self.assertIsNotNone(market)
        self.assertEqual(market["sportsMarketType"], "moneyline")
        self.assertEqual(market["groupItemTitle"], "Match Winner")

    def test_parse_event_extracts_tradable_identity(self):
        record = parse_event(self.event)
        self.assertEqual(record["match_id"], "cs2-lavked-eac-2026-08-29")
        self.assertEqual(record["team_a"], "Lavked")
        self.assertEqual(record["team_b"], "Esport Academy Copenhagen")
        self.assertEqual(record["best_of"], 3)
        self.assertNotEqual(record["token_a"], record["token_b"])
        self.assertEqual(record["pandascore_match_id"], 1648237)
        self.assertTrue(record["scheduled_at"].endswith("Z"))

    def test_tokens_come_from_the_series_market_not_map_one(self):
        record = parse_event(self.event)
        map_one = [
            m for m in self.event["markets"] if m["sportsMarketType"] == "child_moneyline"
        ][0]
        map_tokens = json.loads(map_one["clobTokenIds"])
        self.assertNotIn(record["token_a"], map_tokens)

    def test_event_without_moneyline_is_rejected(self):
        self.event["markets"] = [
            m for m in self.event["markets"] if m["sportsMarketType"] != "moneyline"
        ]
        self.assertIsNone(parse_event(self.event))

    def test_non_binary_outcomes_are_rejected(self):
        for market in self.event["markets"]:
            if market["sportsMarketType"] == "moneyline":
                market["outcomes"] = json.dumps(["A", "B", "C"])
        self.assertIsNone(parse_event(self.event))

    def test_best_of_reads_the_score_field(self):
        self.assertEqual(best_of({"score": "0-0|0-0|Bo5"}), 5)
        self.assertEqual(best_of({"title": "Counter-Strike: X vs Y (BO1) - Cup"}), 1)
        self.assertEqual(best_of({}), 3)

    def test_map_score_counts_only_resolved_markets(self):
        team_a, team_b = "Lavked", "Esport Academy Copenhagen"
        self.assertEqual(
            map_score_from_markets(self.event, team_a, team_b),
            {"maps_a": 0, "maps_b": 0},
        )
        for market in self.event["markets"]:
            if market.get("groupItemTitle") == "Map 1 Winner":
                market["closed"] = True
                market["outcomePrices"] = json.dumps(["1", "0"])
        self.assertEqual(
            map_score_from_markets(self.event, team_a, team_b),
            {"maps_a": 1, "maps_b": 0},
        )

    def test_open_market_at_extreme_price_is_not_a_win(self):
        for market in self.event["markets"]:
            if market.get("groupItemTitle") == "Map 1 Winner":
                market["closed"] = False
                market["outcomePrices"] = json.dumps(["0.995", "0.005"])
        self.assertEqual(
            map_score_from_markets(self.event, "Lavked", "Esport Academy Copenhagen"),
            {"maps_a": 0, "maps_b": 0},
        )

    def test_to_match_validates(self):
        match = to_match(parse_event(self.event), 0.5)
        self.assertEqual(match.source, "polymarket-gamma")
        self.assertEqual(match.best_of, 3)


if __name__ == "__main__":
    unittest.main()
