import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from polytrade_esports.gamma import is_stale, resolution_from_event
from polytrade_esports.resolver import resolve_open_matches
from polytrade_esports.scoring import brier, log_loss, score
from polytrade_esports.storage import Database
from polytrade_esports.timeutil import isoformat, utc_now
from polytrade_esports.types import BookQuote, LiveState, Match

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gamma_cs2_event.json").read_text()
)


def settled(winner_index, closed=True, ended=True):
    event = copy.deepcopy(FIXTURE)
    event["ended"] = ended
    for market in event["markets"]:
        if market["sportsMarketType"] == "moneyline":
            market["closed"] = closed
            prices = ["0", "0"]
            if winner_index is not None:
                prices[winner_index] = "1"
            market["outcomePrices"] = json.dumps(prices)
    return event


class ResolutionParsingTests(unittest.TestCase):
    def test_winner_is_read_from_the_settled_series_market(self):
        self.assertEqual(
            resolution_from_event(settled(0), "Lavked", "Esport Academy Copenhagen"),
            {"winner": "A", "void": False},
        )
        self.assertEqual(
            resolution_from_event(settled(1), "Lavked", "Esport Academy Copenhagen"),
            {"winner": "B", "void": False},
        )

    def test_winner_follows_the_team_name_not_the_list_position(self):
        # Polymarket reorders the outcome pair; the stored A side must still win.
        event = settled(0)
        for market in event["markets"]:
            if market["sportsMarketType"] == "moneyline":
                market["outcomes"] = json.dumps(
                    ["Esport Academy Copenhagen", "Lavked"]
                )
                market["outcomePrices"] = json.dumps(["0", "1"])
        self.assertEqual(
            resolution_from_event(event, "Lavked", "Esport Academy Copenhagen"),
            {"winner": "A", "void": False},
        )

    def test_unfinished_match_does_not_resolve(self):
        self.assertIsNone(
            resolution_from_event(settled(0, ended=False), "Lavked", "Esport Academy Copenhagen")
        )
        self.assertIsNone(
            resolution_from_event(settled(0, closed=False), "Lavked", "Esport Academy Copenhagen")
        )

    def test_undecided_prices_do_not_resolve(self):
        event = settled(0)
        for market in event["markets"]:
            if market["sportsMarketType"] == "moneyline":
                market["outcomePrices"] = json.dumps(["0.5", "0.5"])
        self.assertEqual(
            resolution_from_event(event, "Lavked", "Esport Academy Copenhagen"),
            {"winner": None, "void": True},
        )

    def test_result_for_an_unrelated_team_is_refused(self):
        event = settled(0)
        for market in event["markets"]:
            if market["sportsMarketType"] == "moneyline":
                market["outcomes"] = json.dumps(["Someone Else", "Another Team"])
        self.assertIsNone(
            resolution_from_event(event, "Lavked", "Esport Academy Copenhagen")
        )

    def test_finished_and_ancient_events_are_stale(self):
        self.assertTrue(is_stale({"ended": True, "scheduled_at": None}))
        self.assertTrue(is_stale({"ended": False, "scheduled_at": "2026-05-02T00:00:00Z"}))
        self.assertFalse(
            is_stale({"ended": False, "scheduled_at": isoformat(utc_now())})
        )


class FakeGamma:
    def __init__(self, events):
        self.events = events
        self.requested = []

    def get_event(self, slug):
        self.requested.append(slug)
        return self.events.get(slug)


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "r.sqlite3"))
        self.db.initialize()
        self.db.ensure_account("live-paper")
        # Past the 5h settlement gate, well short of the 14d abandon threshold.
        finished_recently = isoformat(utc_now() - timedelta(hours=8))
        self.db.add_match(
            Match(
                "cs2-lavked-eac-2026-08-29",
                "Lavked",
                "Esport Academy Copenhagen",
                3,
                0.6,
                scheduled_at=finished_recently,
            )
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_finished_match_is_resolved_and_closed(self):
        self.db.update_match_lifecycle(
            "cs2-lavked-eac-2026-08-29", live=True, ended=False
        )
        gamma = FakeGamma({"cs2-lavked-eac-2026-08-29": settled(0)})
        result = resolve_open_matches(self.db, gamma=gamma)
        self.assertEqual(result.resolved, 1)
        self.assertEqual(result.decided[0]["winning_team"], "Lavked")
        self.assertEqual(
            self.db.dashboard_payload()["matches"][0]["winner"], "A"
        )
        row = self.db.dashboard_payload()["matches"][0]
        self.assertEqual((row["live"], row["ended"]), (0, 1))

    def test_finished_unsettled_match_is_pending_not_live(self):
        self.db.update_match_lifecycle(
            "cs2-lavked-eac-2026-08-29", live=True, ended=False
        )
        event = settled(0, closed=False, ended=True)
        result = resolve_open_matches(
            self.db, gamma=FakeGamma({"cs2-lavked-eac-2026-08-29": event})
        )
        row = self.db.dashboard_payload()["matches"][0]
        self.assertEqual(result.pending, 1)
        self.assertEqual((row["live"], row["ended"], row["status"]), (0, 1, "open"))

    def test_resolved_match_is_not_checked_again(self):
        gamma = FakeGamma({"cs2-lavked-eac-2026-08-29": settled(0)})
        resolve_open_matches(self.db, gamma=gamma)
        second = resolve_open_matches(self.db, gamma=gamma)
        self.assertEqual(second.checked, 0, "a settled match must leave the queue")

    def test_confirmed_ended_match_bypasses_the_five_hour_gate(self):
        match_id = "cs2-young-finished-2026-08-29"
        self.db.add_match(
            Match(
                match_id,
                "Lavked",
                "Esport Academy Copenhagen",
                3,
                0.5,
                scheduled_at=isoformat(utc_now()),
            )
        )
        self.db.update_match_lifecycle(match_id, live=False, ended=True)
        result = resolve_open_matches(
            self.db,
            gamma=FakeGamma({match_id: settled(0)}),
            min_age_hours=5.0,
        )
        self.assertEqual(result.resolved, 1)
        self.assertEqual(self.db.match_detail(match_id)["winner"], "A")

    def test_undecided_match_stays_pending(self):
        gamma = FakeGamma({"cs2-lavked-eac-2026-08-29": settled(0, ended=False)})
        result = resolve_open_matches(self.db, gamma=gamma)
        self.assertEqual(result.pending, 1)
        self.assertEqual(result.resolved, 0)

    def test_void_match_is_closed_without_a_winner(self):
        gamma = FakeGamma({"cs2-lavked-eac-2026-08-29": settled(None)})
        result = resolve_open_matches(self.db, gamma=gamma)
        self.assertEqual(result.voided, 1)
        self.assertEqual(result.resolved, 0)

    def test_never_settled_match_is_abandoned_not_polled_forever(self):
        # Polymarket leaves some fixtures unsettled indefinitely; the oldest
        # seen in production was 113 days old and still being re-requested.
        self.db.add_match(
            Match(
                "cs2-ancient-2026-05-06",
                "Cowana",
                "Brawlers",
                3,
                0.5,
                scheduled_at=isoformat(utc_now() - timedelta(days=100)),
            )
        )
        gamma = FakeGamma({})
        result = resolve_open_matches(self.db, gamma=gamma, abandon_after_days=14.0)
        self.assertEqual(result.abandoned, 1)
        self.assertEqual(result.voided, 1)
        self.assertNotIn(
            "cs2-ancient-2026-05-06",
            gamma.requested,
            "an abandoned match must cost no request",
        )
        follow_up = resolve_open_matches(self.db, gamma=FakeGamma({}))
        self.assertNotIn("cs2-ancient-2026-05-06", [
            m for m in follow_up.errors
        ])
        remaining = [
            r["match_id"] for r in self.db.matches_awaiting_resolution()
        ]
        self.assertNotIn("cs2-ancient-2026-05-06", remaining)

    def test_a_recent_unsettled_match_is_not_abandoned(self):
        self.db.add_match(
            Match(
                "cs2-yesterday-2026-08-27",
                "X",
                "Y",
                3,
                0.5,
                scheduled_at=isoformat(utc_now() - timedelta(hours=30)),
            )
        )
        gamma = FakeGamma({"cs2-yesterday-2026-08-27": settled(0, ended=False)})
        result = resolve_open_matches(self.db, gamma=gamma, abandon_after_days=14.0)
        self.assertEqual(result.abandoned, 0)
        self.assertIn("cs2-yesterday-2026-08-27", gamma.requested)

    def test_a_just_finished_match_is_not_checked_yet(self):
        # Measured: nothing settles inside 6h of the start, so asking is waste.
        self.db.add_match(
            Match(
                "cs2-fresh-2026-08-28",
                "X",
                "Y",
                3,
                0.5,
                scheduled_at=isoformat(utc_now() - timedelta(hours=2)),
            )
        )
        gamma = FakeGamma({})
        resolve_open_matches(self.db, gamma=gamma)
        self.assertNotIn("cs2-fresh-2026-08-28", gamma.requested)


class PriorQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "q.sqlite3"))
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def _add(self, match_id, hours_out, liquidity):
        self.db.add_match(
            Match(
                match_id, match_id + "-A", match_id + "-B", 3, 0.5,
                scheduled_at=isoformat(utc_now() + timedelta(hours=hours_out)),
            )
        )
        with self.db.connect() as c:
            c.execute("UPDATE matches SET liquidity=? WHERE match_id=?", (liquidity, match_id))

    def test_the_next_match_to_start_is_priced_first(self):
        # The failure this guards: a big match tomorrow starved a small one
        # starting in minutes, which then went live still showing the seed.
        self._add("soon-small", 0.2, 500.0)
        self._add("later-huge", 20.0, 300000.0)
        queue = [r["match_id"] for r in self.db.matches_needing_prior(limit=2)]
        self.assertEqual(queue[0], "soon-small")

    def test_liquidity_is_a_floor_not_a_ranking(self):
        self._add("soon-junk", 0.2, 10.0)
        self._add("soon-real", 0.5, 5000.0)
        queue = [
            r["match_id"] for r in self.db.matches_needing_prior(limit=5, min_liquidity=1000.0)
        ]
        self.assertEqual(queue, ["soon-real"])

    def test_a_match_that_already_started_is_not_priced(self):
        # Sorting soonest-first without this put yesterday's fixtures at the
        # head of the queue and spent budget forecasting finished matches.
        self._add("yesterday", -16.0, 5000.0)
        self._add("tomorrow", 20.0, 5000.0)
        queue = [r["match_id"] for r in self.db.matches_needing_prior(limit=5)]
        self.assertEqual(queue, ["tomorrow"])

    def test_a_slightly_late_start_still_counts_as_pre_match(self):
        self._add("just-slipped", -0.1, 5000.0)
        queue = [r["match_id"] for r in self.db.matches_needing_prior(limit=5)]
        self.assertEqual(queue, ["just-slipped"])

    def test_a_live_match_is_never_given_a_pre_match_prior(self):
        self._add("already-live", -1.0, 5000.0)
        with self.db.connect() as c:
            c.execute("UPDATE matches SET live=1 WHERE match_id=?", ("already-live",))
        self.assertEqual(self.db.matches_needing_prior(limit=5), [])

    def test_an_already_priced_match_leaves_the_queue(self):
        self._add("priced", 1.0, 5000.0)
        self.db.apply_prior(
            match_id="priced",
            parsed={
                "probability_team_a": 0.6, "raw_probability_team_a": 0.6,
                "confidence": "low", "reasoning_summary": "t",
                "evidence_cutoff_at": isoformat(utc_now()), "prompt_version": "t",
                "usage": {},
            },
            provider="test", model="test-model",
        )
        self.assertEqual(self.db.matches_needing_prior(limit=5), [])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "s.sqlite3"))
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def _priced_match(self, match_id, ai, market, winner):
        old = isoformat(utc_now().replace(year=utc_now().year - 1))
        self.db.add_match(Match(match_id, "A Team", "B Team", 3, 0.5, scheduled_at=old))
        prior_id = self.db.apply_prior(
            match_id=match_id,
            parsed={
                "probability_team_a": ai,
                "raw_probability_team_a": ai,
                "confidence": "medium",
                "reasoning_summary": "test",
                "evidence_cutoff_at": old,
                "prompt_version": "t",
                "usage": {},
            },
            provider="test",
            model="test-model",
        )
        self.db.set_prior_market_probability(prior_id, market)
        self.db.resolve_match(match_id, winner, isoformat(utc_now()))

    def test_metrics_match_their_definitions(self):
        self.assertAlmostEqual(brier(0.7, 1), 0.09)
        self.assertAlmostEqual(brier(0.7, 0), 0.49)
        self.assertGreater(log_loss(0.01, 1), log_loss(0.4, 1))

    def test_a_sharper_ai_beats_the_market(self):
        self._priced_match("m1", ai=0.9, market=0.6, winner="A")
        self._priced_match("m2", ai=0.1, market=0.4, winner="B")
        report = score(self.db)
        self.assertEqual(report["ai"]["n"], 2)
        self.assertLess(report["ai"]["brier"], report["market"]["brier"])
        self.assertEqual(report["verdict"], "AI ahead of the market")
        self.assertEqual(report["ai"]["accuracy"], 1.0)

    def test_a_confidently_wrong_ai_loses(self):
        self._priced_match("m1", ai=0.95, market=0.5, winner="B")
        report = score(self.db)
        self.assertGreater(report["ai"]["brier"], report["market"]["brier"])
        self.assertEqual(report["verdict"], "market ahead of the AI")
        self.assertFalse(report["ai_beats_coin_flip"])

    def test_small_samples_are_flagged_unreliable(self):
        self._priced_match("m1", ai=0.9, market=0.6, winner="A")
        self.assertFalse(score(self.db)["reliable"])

    def test_unresolved_matches_are_not_scored(self):
        old = isoformat(utc_now().replace(year=utc_now().year - 1))
        self.db.add_match(Match("m9", "A Team", "B Team", 3, 0.5, scheduled_at=old))
        self.db.apply_prior(
            match_id="m9",
            parsed={
                "probability_team_a": 0.8,
                "raw_probability_team_a": 0.8,
                "confidence": "high",
                "reasoning_summary": "test",
                "evidence_cutoff_at": old,
                "prompt_version": "t",
                "usage": {},
            },
            provider="test",
            model="test-model",
        )
        self.assertEqual(score(self.db)["ai"]["n"], 0)

    def test_baseline_never_uses_a_price_from_after_the_prior(self):
        from polytrade_esports.types import BookQuote

        old = isoformat(utc_now() - timedelta(hours=5))
        self.db.add_match(Match("m7", "A Team", "B Team", 3, 0.5, scheduled_at=old))
        prior_at = isoformat(utc_now() - timedelta(hours=2))
        # Only a book from *after* the prior exists.
        later = isoformat(utc_now() - timedelta(hours=1))
        self.db.record_book(
            BookQuote("m7", 0.40, 0.42, 0.58, 0.60, later, later, "test").normalized()
        )
        self.assertIsNone(
            self.db.nearest_market_probability("m7", prior_at),
            "a later price would hand the market a look ahead",
        )
        earlier = isoformat(utc_now() - timedelta(hours=3))
        self.db.record_book(
            BookQuote("m7", 0.30, 0.32, 0.68, 0.70, earlier, earlier, "test").normalized()
        )
        self.assertAlmostEqual(
            self.db.nearest_market_probability("m7", prior_at), 0.31
        )

    def test_detail_page_and_scorer_agree_on_scoreability(self):
        from polytrade_esports.types import BookQuote

        old = isoformat(utc_now() - timedelta(hours=6))
        self.db.add_match(Match("m6", "A Team", "B Team", 3, 0.5, scheduled_at=old))
        book_at = isoformat(utc_now() - timedelta(hours=4))
        self.db.record_book(
            BookQuote("m6", 0.44, 0.46, 0.54, 0.56, book_at, book_at, "test").normalized()
        )
        self.db.apply_prior(
            match_id="m6",
            parsed={
                "probability_team_a": 0.8, "raw_probability_team_a": 0.8,
                "confidence": "high", "reasoning_summary": "t",
                "evidence_cutoff_at": old, "prompt_version": "t", "usage": {},
            },
            provider="test", model="test-model",
        )
        # The column was never written, so both paths must reach the same
        # fallback rather than one claiming the match cannot be scored.
        detail = self.db.match_detail("m6")
        self.assertAlmostEqual(detail["prior"]["market_probability_a"], 0.45)
        self.db.resolve_match("m6", "A", isoformat(utc_now()))
        self.assertEqual(score(self.db)["ai"]["n"], 1)

    def test_a_match_without_a_baseline_is_skipped_not_invented(self):
        old = isoformat(utc_now().replace(year=utc_now().year - 1))
        self.db.add_match(Match("m8", "A Team", "B Team", 3, 0.5, scheduled_at=old))
        self.db.apply_prior(
            match_id="m8",
            parsed={
                "probability_team_a": 0.8,
                "raw_probability_team_a": 0.8,
                "confidence": "high",
                "reasoning_summary": "test",
                "evidence_cutoff_at": old,
                "prompt_version": "t",
                "usage": {},
            },
            provider="test",
            model="test-model",
        )
        self.db.resolve_match("m8", "A", isoformat(utc_now()))
        report = score(self.db)
        self.assertEqual(report["ai"]["n"], 0)
        self.assertEqual(report["missing_baseline"], 1)


if __name__ == "__main__":
    unittest.main()
