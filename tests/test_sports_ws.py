import json
import unittest
from datetime import timedelta

from polytrade_esports.sports_ws import (
    MAPS_ONLY_SOURCE,
    SOURCE,
    TERMINAL_SOURCE,
    SportsWebSocketAdapter,
    build_state,
    parse_update,
)
from polytrade_esports.timeutil import isoformat, parse_timestamp, utc_now


def update_payload(**overrides):
    payload = {
        "gameId": 1648241,
        "leagueAbbreviation": "cs2",
        "homeTeam": "G2 Ares",
        "awayTeam": "Rune Eaters",
        "status": "running",
        "score": "6-1|0-1|Bo3",
        "period": "2/3",
        "live": True,
        "ended": False,
        "last_update": "2026-08-29T00:00:00Z",
    }
    payload.update(overrides)
    return payload


class SportsParserTests(unittest.TestCase):
    def test_parses_cs2_rounds_maps_and_timestamp(self):
        update = parse_update(
            json.dumps(update_payload()),
            received_at="2026-08-29T00:00:01Z",
        )
        self.assertIsNotNone(update)
        self.assertEqual(update.game_id, "1648241")
        self.assertEqual(
            (update.rounds_home, update.rounds_away),
            (6, 1),
        )
        self.assertEqual((update.maps_home, update.maps_away), (0, 1))
        self.assertEqual(update.best_of, 3)
        self.assertTrue(update.rounds_available)
        self.assertEqual(update.source_at, "2026-08-29T00:00:00Z")

    def test_orients_an_away_team_a_into_market_order(self):
        stamp = isoformat(utc_now())
        update = parse_update(
            update_payload(last_update=stamp),
            received_at=stamp,
        )
        state = build_state(
            update,
            match_id="cs2-rune-g2",
            team_a="Rune Eaters",
            team_b="G2 Ares",
        )
        self.assertEqual((state.maps_a, state.maps_b), (1, 0))
        self.assertEqual((state.rounds_a, state.rounds_b), (1, 6))
        self.assertEqual(state.current_map, "Map 2")
        self.assertEqual(state.source, SOURCE)

    def test_rejects_non_cs2_and_malformed_scores(self):
        self.assertIsNone(parse_update("ping"))
        self.assertIsNone(parse_update(update_payload(leagueAbbreviation="nba")))
        self.assertIsNone(parse_update(update_payload(score="6-1")))
        self.assertIsNone(parse_update(update_payload(score="6-1|3-0|Bo3")))

    def test_placeholder_rounds_keep_maps_but_are_not_round_level(self):
        stamp = isoformat(utc_now())
        update = parse_update(
            update_payload(score="000-000|1-1|Bo3", last_update=stamp),
            received_at=stamp,
        )
        state = build_state(update, "m1", "G2 Ares", "Rune Eaters")
        self.assertFalse(update.rounds_available)
        self.assertEqual((state.maps_a, state.maps_b), (1, 1))
        self.assertEqual((state.rounds_a, state.rounds_b), (0, 0))
        self.assertEqual(state.source, MAPS_ONLY_SOURCE)

    def test_terminal_score_cannot_unlock_a_post_match_entry(self):
        stamp = isoformat(utc_now())
        update = parse_update(
            update_payload(score="13-4|2-0|Bo3", last_update=stamp),
            received_at=stamp,
        )
        state = build_state(update, "m1", "G2 Ares", "Rune Eaters")
        self.assertEqual(state.source, TERMINAL_SOURCE)

    def test_provider_timestamp_is_clamped_to_observation(self):
        update = parse_update(
            update_payload(last_update="2026-08-29T00:01:00Z"),
            received_at="2026-08-29T00:00:00Z",
        )
        self.assertEqual(update.source_at, "2026-08-29T00:00:00Z")

    def test_team_identity_mismatch_fails_closed(self):
        update = parse_update(
            update_payload(),
            received_at="2026-08-29T00:00:01Z",
        )
        with self.assertRaisesRegex(ValueError, "do not match"):
            build_state(update, "m1", "Wrong A", "Wrong B")

    def test_generic_gaming_suffix_is_a_cosmetic_difference(self):
        stamp = isoformat(utc_now())
        update = parse_update(
            update_payload(
                homeTeam="ALKA GAMING",
                awayTeam="BESTIA Academy",
                score="4-1|0-0|Bo3",
                last_update=stamp,
            ),
            received_at=stamp,
        )
        state = build_state(update, "m1", "ALKA", "BESTIA Academy")
        self.assertEqual((state.rounds_a, state.rounds_b), (4, 1))


class SportsAdapterCacheTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SportsWebSocketAdapter(max_age_seconds=90)
        self.now = utc_now()
        stamp = isoformat(self.now)
        self.adapter.ingest(
            update_payload(last_update=stamp),
            received_at=stamp,
        )

    def _connect_for_test(self):
        with self.adapter._lock:
            self.adapter._connected = True

    def test_disconnected_cache_is_not_trusted(self):
        state = self.adapter.state_for(
            "1648241", "m1", "G2 Ares", "Rune Eaters", now=self.now
        )
        self.assertIsNone(state)

    def test_connected_fresh_cache_returns_state(self):
        self._connect_for_test()
        state = self.adapter.state_for(
            "1648241", "m1", "G2 Ares", "Rune Eaters", now=self.now
        )
        self.assertEqual((state.rounds_a, state.rounds_b), (6, 1))

    def test_stale_cache_is_not_trusted(self):
        self._connect_for_test()
        state = self.adapter.state_for(
            "1648241",
            "m1",
            "G2 Ares",
            "Rune Eaters",
            now=self.now + timedelta(seconds=91),
        )
        self.assertIsNone(state)

    def test_ended_update_is_not_live_state(self):
        stamp = isoformat(self.now + timedelta(seconds=1))
        self.adapter.ingest(
            update_payload(live=False, ended=True, last_update=stamp),
            received_at=stamp,
        )
        self._connect_for_test()
        self.assertIsNone(
            self.adapter.state_for(
                "1648241",
                "m1",
                "G2 Ares",
                "Rune Eaters",
                now=parse_timestamp(stamp),
            )
        )


if __name__ == "__main__":
    unittest.main()
