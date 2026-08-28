import copy
import json
import tempfile
import unittest
from pathlib import Path

import polytrade_esports.collector as collector_module
from polytrade_esports.collector import CollectorConfig, GameDetailGate, run_cycle
from polytrade_esports.storage import Database
from polytrade_esports.timeutil import isoformat, utc_now
from polytrade_esports.types import BookQuote

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gamma_cs2_event.json").read_text()
)


class FakeGamma:
    def __init__(self, events, details=None):
        self.events = events
        self.details = details or {}

    def cs2_events(self, max_pages=6):
        return copy.deepcopy(self.events)

    def get_event(self, slug):
        if slug in self.details:
            return copy.deepcopy(self.details[slug])
        for event in self.events:
            if event.get("slug") == slug:
                return copy.deepcopy(event)
        return None


class FakeBooks:
    def __init__(self, ask_a=0.30, ask_b=0.72):
        self.ask_a = ask_a
        self.ask_b = ask_b
        self.calls = []

    def get_pair(self, match_id, token_a, token_b):
        self.calls.append(match_id)
        now = isoformat(utc_now())
        return BookQuote(
            match_id=match_id,
            bid_a=self.ask_a - 0.01,
            ask_a=self.ask_a,
            bid_b=self.ask_b - 0.01,
            ask_b=self.ask_b,
            source_at=now,
            observed_at=now,
            source="test",
        ).normalized()


def live_event():
    event = copy.deepcopy(FIXTURE)
    event["live"] = True
    event["ended"] = False
    event["startTime"] = isoformat(utc_now())
    return event


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "c.sqlite3"))
        self.config = CollectorConfig(pandascore_token="")

    def tearDown(self):
        self.temp.cleanup()

    def test_cycle_discovers_and_forecasts_a_live_match(self):
        result = run_cycle(self.db, self.config, gamma=FakeGamma([live_event()]), books=FakeBooks())
        self.assertEqual(result.discovered, 1)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.ticked, 1)
        self.assertEqual(result.errors, [])

    def test_seed_prior_never_opens_a_paper_position(self):
        # Market says 30c for team A while the seed model sits at 0.50: a large
        # apparent edge that exists only because nothing has been researched.
        result = run_cycle(self.db, self.config, gamma=FakeGamma([live_event()]), books=FakeBooks())
        self.assertFalse(result.forecasts[0]["paper_enabled"])
        self.assertGreater(result.forecasts[0]["edge_a"], 0.15)
        self.assertEqual(self.db.dashboard_payload()["counts"]["trades"], 0)

    def test_applied_prior_re_enables_the_paper_engine(self):
        run_cycle(self.db, self.config, gamma=FakeGamma([live_event()]), books=FakeBooks())
        self.db.apply_prior(
            match_id=FIXTURE["slug"],
            parsed={
                "probability_team_a": 0.7,
                "raw_probability_team_a": 0.7,
                "confidence": "medium",
                "reasoning_summary": "test",
                "evidence_cutoff_at": isoformat(utc_now()),
                "prompt_version": "test",
                "usage": {"estimated_cost_usd": 0.004},
            },
            provider="test",
            model="test-model",
        )
        result = run_cycle(self.db, self.config, gamma=FakeGamma([live_event()]), books=FakeBooks())
        self.assertTrue(result.forecasts[0]["paper_enabled"])
        self.assertGreater(self.db.dashboard_payload()["counts"]["trades"], 0)

    def test_prior_is_recorded_and_surfaced_to_the_dashboard(self):
        run_cycle(self.db, self.config, gamma=FakeGamma([live_event()]), books=FakeBooks())
        self.db.apply_prior(
            match_id=FIXTURE["slug"],
            parsed={
                "probability_team_a": 0.7,
                "raw_probability_team_a": 0.7,
                "confidence": "high",
                "reasoning_summary": "Lavked hold the map-pool edge.",
                "key_factors": ["map pool"],
                "evidence_cutoff_at": isoformat(utc_now()),
                "prompt_version": "cs2-prior-v1",
                "usage": {"estimated_cost_usd": 0.004},
            },
            provider="deepseek",
            model="deepseek-v4-pro",
        )
        row = self.db.dashboard_payload()["matches"][0]
        self.assertAlmostEqual(row["prior_probability_llm"], 0.7)
        self.assertEqual(row["prior_confidence"], "high")
        self.assertEqual(row["key_factors"], ["map pool"])
        self.assertTrue(row["prior_source"].startswith("llm:"))

    def test_ended_match_is_not_ticked(self):
        event = live_event()
        event["ended"] = True
        books = FakeBooks()
        result = run_cycle(self.db, self.config, gamma=FakeGamma([event]), books=books)
        self.assertEqual(result.ticked, 0)
        self.assertEqual(books.calls, [])

    def test_live_match_moves_to_finished_pending_before_settlement(self):
        running = live_event()
        run_cycle(self.db, self.config, gamma=FakeGamma([running]), books=FakeBooks())

        finished = copy.deepcopy(running)
        finished["live"] = False
        finished["ended"] = True
        result = run_cycle(
            self.db, self.config, gamma=FakeGamma([finished]), books=FakeBooks()
        )

        payload = self.db.dashboard_payload()
        row = payload["matches"][0]
        self.assertEqual(result.finished, 1)
        self.assertEqual((row["live"], row["ended"], row["status"]), (0, 1, "open"))
        self.assertEqual(payload["counts"]["live"], 0)
        self.assertEqual(payload["counts"]["pending"], 1)

    def test_settled_event_records_final_maps_and_resolves_immediately(self):
        running = live_event()
        run_cycle(self.db, self.config, gamma=FakeGamma([running]), books=FakeBooks())

        finished = copy.deepcopy(running)
        finished["live"] = False
        finished["ended"] = True
        finished["score"] = "000-000|2-0|Bo3"
        for market in finished["markets"]:
            if market.get("groupItemTitle") in ("Map 1 Winner", "Map 2 Winner"):
                market["closed"] = True
                market["outcomePrices"] = json.dumps(["1", "0"])
            if market.get("sportsMarketType") == "moneyline":
                market["closed"] = True
                market["outcomePrices"] = json.dumps(["1", "0"])

        result = run_cycle(
            self.db,
            self.config,
            gamma=FakeGamma([finished]),
            books=FakeBooks(),
            cycle_index=1,
        )

        row = self.db.dashboard_payload()["matches"][0]
        self.assertEqual(result.resolved, 1)
        self.assertEqual((row["status"], row["winner"]), ("resolved", "A"))
        self.assertEqual((row["maps_a"], row["maps_b"]), (2, 0))
        self.assertEqual(row["current_map"], "FINAL")
        state = self.db.latest_state(FIXTURE["slug"])
        self.assertEqual(state.source, "polymarket-gamma-final")
        detail = self.db.match_detail(FIXTURE["slug"])
        self.assertEqual((detail["latest"]["maps_a"], detail["latest"]["maps_b"]), (2, 0))

    def test_live_match_missing_from_discovery_is_reconciled_by_slug(self):
        running = live_event()
        run_cycle(self.db, self.config, gamma=FakeGamma([running]), books=FakeBooks())
        finished = copy.deepcopy(running)
        finished["live"] = False
        finished["ended"] = True

        run_cycle(
            self.db,
            self.config,
            gamma=FakeGamma([], details={running["slug"]: finished}),
            books=FakeBooks(),
        )
        row = self.db.dashboard_payload()["matches"][0]
        self.assertEqual((row["live"], row["ended"]), (0, 1))

    def test_far_future_match_is_discovered_but_not_ticked(self):
        event = copy.deepcopy(FIXTURE)
        event["live"] = False
        event["startTime"] = "2099-01-01T00:00:00Z"
        books = FakeBooks()
        result = run_cycle(self.db, self.config, gamma=FakeGamma([event]), books=books)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.ticked, 0)
        self.assertEqual(books.calls, [])

    def test_second_cycle_updates_rather_than_duplicating(self):
        gamma = FakeGamma([live_event()])
        run_cycle(self.db, self.config, gamma=gamma, books=FakeBooks())
        result = run_cycle(self.db, self.config, gamma=gamma, books=FakeBooks())
        self.assertEqual(result.inserted, 0)
        self.assertEqual(self.db.dashboard_payload()["counts"]["matches"], 1)

    def test_reused_slug_with_different_teams_is_flagged_not_overwritten(self):
        run_cycle(self.db, self.config, gamma=FakeGamma([live_event()]), books=FakeBooks())
        swapped = live_event()
        for market in swapped["markets"]:
            if market["sportsMarketType"] == "moneyline":
                market["outcomes"] = json.dumps(["Someone Else", "Another Team"])
        result = run_cycle(self.db, self.config, gamma=FakeGamma([swapped]), books=FakeBooks())
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(self.db.get_match(FIXTURE["slug"]).team_a, "Lavked")

    def test_one_broken_book_does_not_abort_the_sweep(self):
        first = live_event()
        second = live_event()
        second["slug"] = "cs2-second-match-2026-08-29"
        second["id"] = "999999"

        class FlakyBooks(FakeBooks):
            def get_pair(self, match_id, token_a, token_b):
                if match_id == FIXTURE["slug"]:
                    raise RuntimeError("book unavailable")
                return FakeBooks.get_pair(self, match_id, token_a, token_b)

        result = run_cycle(self.db, self.config, gamma=FakeGamma([first, second]), books=FlakyBooks())
        self.assertEqual(result.ticked, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("book unavailable", result.errors[0])

    def test_plan_gated_game_detail_is_requested_once_then_dropped(self):
        # Free-tier PandaScore refuses /csgo/games/{id}. Retrying it on every
        # live match every cycle would burn the request quota for nothing.
        from polytrade_esports.pandascore import PandaScoreError

        class RefusingPanda:
            def __init__(self):
                self.game_calls = 0

            def running_matches(self, per_page=50):
                return [
                    {
                        "id": 1648237,
                        "opponents": [
                            {"opponent": {"id": 1, "name": "Lavked"}},
                            {"opponent": {"id": 2, "name": "Esport Academy Copenhagen"}},
                        ],
                        "results": [
                            {"team_id": 1, "score": 1},
                            {"team_id": 2, "score": 0},
                        ],
                        "games": [{"id": 7, "position": 2, "status": "running"}],
                    }
                ]

            def game(self, game_id):
                self.game_calls += 1
                raise PandaScoreError("Access Denied", status=403)

        panda = RefusingPanda()
        collector_module._GAME_DETAIL_GATE = GameDetailGate()
        original = collector_module.PandaScoreClient
        collector_module.PandaScoreClient = lambda token: panda
        try:
            config = CollectorConfig(pandascore_token="x")
            gamma = FakeGamma([live_event()])
            first = run_cycle(self.db, config, gamma=gamma, books=FakeBooks())
            second = run_cycle(self.db, config, gamma=gamma, books=FakeBooks())
        finally:
            collector_module.PandaScoreClient = original
            collector_module._GAME_DETAIL_GATE = None

        self.assertEqual(panda.game_calls, 1, "gate should stop the retry")
        self.assertEqual(first.ticked, 1)
        self.assertEqual(second.ticked, 1)
        self.assertEqual(second.errors, [], "the refusal must not repeat every cycle")
        self.assertEqual(second.notices, [], "the notice must not repeat either")
        self.assertEqual(
            self.db.latest_collector_run()["notices"],
            [collector_module.MAPS_ONLY_NOTICE],
            "the effective maps-only capability must remain visible",
        )
        self.assertEqual(
            first.errors, [], "a plan limitation is a notice, not a failure"
        )
        self.assertEqual(len(first.notices), 1)
        self.assertEqual(
            self.db.latest_collector_run()["status"],
            "completed",
            "a plan limitation must not mark the run partial",
        )

    def test_maps_come_from_the_provider_when_available(self):
        class MapsOnlyPanda:
            def running_matches(self, per_page=50):
                return [
                    {
                        "id": 1648237,
                        "opponents": [
                            {"opponent": {"id": 1, "name": "Lavked"}},
                            {"opponent": {"id": 2, "name": "Esport Academy Copenhagen"}},
                        ],
                        "results": [
                            {"team_id": 1, "score": 1},
                            {"team_id": 2, "score": 0},
                        ],
                        "games": [{"id": 7, "position": 2, "status": "running"}],
                    }
                ]

            def game(self, game_id):
                raise AssertionError("gate should be closed")

        collector_module._GAME_DETAIL_GATE = GameDetailGate()
        collector_module._GAME_DETAIL_GATE.close("plan")
        original = collector_module.PandaScoreClient
        collector_module.PandaScoreClient = lambda token: MapsOnlyPanda()
        try:
            run_cycle(
                self.db,
                CollectorConfig(pandascore_token="x"),
                gamma=FakeGamma([live_event()]),
                books=FakeBooks(),
            )
        finally:
            collector_module.PandaScoreClient = original
            collector_module._GAME_DETAIL_GATE = None

        row = self.db.dashboard_payload()["matches"][0]
        self.assertEqual((row["maps_a"], row["maps_b"]), (1, 0))
        self.assertEqual(row["current_map"], "MAP 2")

    def test_run_is_recorded_for_freshness_reporting(self):
        run_cycle(self.db, self.config, gamma=FakeGamma([live_event()]), books=FakeBooks())
        run = self.db.latest_collector_run()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["ticked"], 1)
        self.assertIsNotNone(run["finished_at"])


if __name__ == "__main__":
    unittest.main()
