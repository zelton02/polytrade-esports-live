import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from polytrade_esports.cli import build_parser
from polytrade_esports.llm import BackendResponse
from polytrade_esports.shadow_panel import (
    PANEL_ROLES,
    PANEL_VERSION,
    build_shadow_prompt,
    robust_consensus,
    run_shadow_panels,
)
from polytrade_esports.scoring import TRADEABLE_LIQUIDITY, shadow_score
from polytrade_esports.storage import Database
from polytrade_esports.timeutil import isoformat, utc_now
from polytrade_esports.types import Match


def member_payload(probability):
    return json.dumps(
        {
            "probability_team_a": probability,
            "confidence": "medium",
            "reasoning_summary": "Role-specific evidence assessment.",
            "key_factors": ["recent results", "roster stability"],
            "supporting_evidence": [],
            "assumptions": ["Listed roster starts."],
        }
    )


class FakeBackend:
    web_research = False

    def __init__(self, probabilities):
        self.probabilities = iter(probabilities)
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return BackendResponse(
            member_payload(next(self.probabilities)),
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "api_calls": 1,
                "estimated_cost_usd": 0.01,
            },
        )


class FakeBooks:
    def __init__(self, midpoint):
        self.midpoint = midpoint
        self.calls = []

    def get_pair(self, match_id, token_a, token_b):
        self.calls.append((match_id, token_a, token_b))

        class Quote:
            midpoint_a = self.midpoint

        return Quote()


class PromptIsolationTests(unittest.TestCase):
    def test_member_prompt_structurally_excludes_market_fields(self):
        record = {
            "team_a": "NAVI",
            "team_b": "M80",
            "best_of": 3,
            "scheduled_at": "2026-09-01T12:00:00Z",
            "context": "Market says ask=0.123456 and BUY NOW",
            "prior_probability_a": 0.876543,
            "liquidity": 987654,
            "token_a": "secret-market-token",
            "verified_facts": "NAVI: roster verified before cutoff",
        }
        prompt = build_shadow_prompt(
            record, "2026-09-01T10:00:00Z", PANEL_ROLES[0], web_research=False
        )
        for forbidden in (
            "0.123456", "0.876543", "987654", "secret-market-token", "BUY NOW"
        ):
            self.assertNotIn(forbidden, prompt)
        self.assertIn("team-a-case", prompt)
        self.assertIn("no web access", prompt)


class ConsensusTests(unittest.TestCase):
    def test_median_consensus_resists_one_extreme_member(self):
        result = robust_consensus([0.50, 0.51, 0.52, 0.95])
        self.assertAlmostEqual(result["probability_a"], 0.515)
        self.assertAlmostEqual(result["spread"], 0.45)
        self.assertLess(result["uncertainty_low_a"], result["probability_a"])
        self.assertGreater(result["uncertainty_high_a"], result["probability_a"])

    def test_three_members_are_required_for_a_robust_result(self):
        with self.assertRaisesRegex(ValueError, "at least 3"):
            robust_consensus([0.4, 0.6])


class ShadowBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "shadow.sqlite3"))
        self.db.initialize()
        self.scheduled_at = isoformat(utc_now() + timedelta(hours=2))
        self.db.add_match(
            Match(
                "m-shadow",
                "Alpha",
                "Bravo",
                3,
                0.5,
                token_a="token-a-private",
                token_b="token-b-private",
                scheduled_at=self.scheduled_at,
            )
        )
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE matches
                SET context='ask=0.123456; market strongly favors Alpha', liquidity=5000
                WHERE match_id='m-shadow'
                """
            )
        facts = {
            "page": "Alpha",
            "roster": [{"id": "player", "joined": "2026-01-01"}],
            "recent": [
                {
                    "played_at": "2026-08-20",
                    "tier": "2",
                    "score": "2-1",
                    "opponent": "Other",
                }
            ],
        }
        self.db.store_team_facts("Alpha", facts)
        self.db.store_team_facts("Bravo", dict(facts, page="Bravo"))
        self.formal_prior_id = self.db.apply_prior(
            "m-shadow",
            {
                "probability_team_a": 0.61,
                "raw_probability_team_a": 0.61,
                "confidence": "medium",
                "reasoning_summary": "Existing production prior.",
                "evidence_cutoff_at": isoformat(utc_now()),
                "prompt_version": "production-test",
                "usage": {"estimated_cost_usd": 5.0},
            },
            provider="test",
            model="production-model",
            grounded_teams=2,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_panel_is_persisted_but_never_applied_or_traded(self):
        backend = FakeBackend([0.62, 0.38, 0.55, 0.51])
        books = FakeBooks(0.60)
        summary, created = run_shadow_panels(
            self.db,
            backend,
            limit=1,
            daily_run_limit=2,
            monthly_budget_usd=1.0,
            max_cost_per_run=0.20,
            provider="fake",
            model="panel-model",
            backend_name="fake",
            books=books,
            use_liquipedia=False,
        )

        self.assertEqual(summary["runs_created"], 1)
        self.assertEqual(summary["usage"]["api_calls"], 4)
        self.assertEqual(created[0]["status"], "completed")
        self.assertFalse(created[0]["applied"])
        self.assertAlmostEqual(created[0]["consensus_probability_a"], 0.53)
        self.assertAlmostEqual(created[0]["market_probability_a"], 0.60)

        match = self.db.get_match("m-shadow")
        self.assertAlmostEqual(match.prior_probability_a, 0.61)
        self.assertEqual(self.db.latest_prior("m-shadow")["prior_id"], self.formal_prior_id)
        run = self.db.latest_shadow_panel_run("m-shadow")
        self.assertEqual(run["panel_version"], PANEL_VERSION)
        self.assertEqual(run["applied"], 0)
        self.assertTrue(all(member["applied"] == 0 for member in run["members"]))
        self.assertEqual([member["role"] for member in run["members"]], [
            role.name for role in PANEL_ROLES
        ])
        self.assertTrue(all("0.123456" not in prompt for prompt in backend.prompts))
        self.assertTrue(all("token-a-private" not in prompt for prompt in backend.prompts))

        with self.db.connect() as connection:
            forecast_count = connection.execute("SELECT count(*) FROM forecasts").fetchone()[0]
            order_count = connection.execute("SELECT count(*) FROM paper_orders").fetchone()[0]
        self.assertEqual(forecast_count, 0)
        self.assertEqual(order_count, 0)

    def test_shadow_budget_is_separate_from_formal_prior_cost(self):
        # The formal prior above cost $5, but the shadow-only ledger starts at 0.
        self.assertEqual(self.db.shadow_panel_cost_since("2000-01-01T00:00:00Z"), 0.0)
        self.assertEqual(self.db.count_shadow_panel_runs_since("2000-01-01T00:00:00Z"), 0)

    def test_panel_does_not_start_inside_pre_match_lead_window(self):
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE matches SET scheduled_at=? WHERE match_id='m-shadow'",
                (isoformat(utc_now() + timedelta(minutes=5)),),
            )
        summary, candidates = run_shadow_panels(
            self.db,
            FakeBackend([]),
            limit=1,
            daily_run_limit=2,
            min_lead_minutes=10,
            dry_run=True,
        )
        self.assertEqual(summary["candidates_selected"], 0)
        self.assertEqual(candidates, [])

    def test_schema_addition_is_idempotent_without_version_takeover(self):
        with self.db.connect() as connection:
            version_before = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        self.db.initialize()
        with self.db.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            version_after = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(version_before, version_after)
        self.assertTrue({"shadow_panel_runs", "shadow_panel_members"}.issubset(tables))


class ShadowCliTests(unittest.TestCase):
    def test_command_has_independent_run_and_cost_limits(self):
        args = build_parser().parse_args(
            [
                "shadow-panel",
                "--limit", "3",
                "--daily-run-limit", "7",
                "--monthly-budget-usd", "2.5",
                "--max-cost-per-run", "0.25",
                "--min-lead-minutes", "15",
                "--loop-seconds", "900",
                "--cached-facts-only",
                "--dry-run",
            ]
        )
        self.assertEqual(args.limit, 3)
        self.assertEqual(args.daily_run_limit, 7)
        self.assertEqual(args.monthly_budget_usd, 2.5)
        self.assertEqual(args.max_cost_per_run, 0.25)
        self.assertEqual(args.min_lead_minutes, 15.0)
        self.assertEqual(args.loop_seconds, 900.0)
        self.assertTrue(args.cached_facts_only)


if __name__ == "__main__":
    unittest.main()


class ShadowScoringTests(unittest.TestCase):
    """The cohort must be evaluable before three months of it accumulate."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "score.sqlite3"))
        self.db.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def _scored_run(
        self, match_id, consensus, market, winner, liquidity,
        members=(), model="deepseek-v4-pro", resolve=True,
    ):
        past = isoformat(utc_now() - timedelta(days=1))
        self.db.add_match(
            Match(match_id, "Alpha", "Bravo", 3, 0.5, scheduled_at=past)
        )
        run_id = self.db.begin_shadow_panel_run(
            match_id=match_id,
            evidence_cutoff_at=past,
            panel_version=PANEL_VERSION,
            provider="deepseek",
            model=model,
            backend="deepseek",
            grounded_teams=2,
            liquidity=liquidity,
        )
        for index, probability in enumerate(members):
            self.db.record_shadow_panel_member(
                run_id=run_id,
                role=PANEL_ROLES[index].name,
                prompt_sha256="%064d" % index,
                parsed={
                    "probability_team_a": probability,
                    "confidence": "medium",
                    "reasoning_summary": "r",
                    "key_factors": [],
                    "supporting_evidence": [],
                    "assumptions": [],
                    "usage": {},
                    "raw_response": "{}",
                },
            )
        self.db.finish_shadow_panel_run(
            run_id=run_id,
            status="completed",
            consensus={
                "probability_a": consensus,
                "uncertainty_low_a": max(0.01, consensus - 0.05),
                "uncertainty_high_a": min(0.99, consensus + 0.05),
                "spread": 0.04,
                "mad": 0.01,
            },
            market_probability_a=market,
            market_captured_at=past,
            usage={},
            errors=[],
        )
        if resolve:
            self.db.resolve_match(match_id, winner, isoformat(utc_now()))
        return run_id

    def test_match_detail_carries_every_panel_run_with_its_members(self):
        self._scored_run(
            "m1", 0.49, 0.425, "A", liquidity=31000.0,
            members=(0.48, 0.48, 0.50, 0.50), model="deepseek-v4-pro",
        )
        runs = self.db.shadow_panel_for_match("m1")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["model"], "deepseek-v4-pro")
        self.assertAlmostEqual(runs[0]["consensus_probability_a"], 0.49)
        self.assertAlmostEqual(runs[0]["market_probability_a"], 0.425)
        roles = [member["role"] for member in runs[0]["members"]]
        self.assertEqual(roles, [role.name for role in PANEL_ROLES])
        detail = self.db.match_detail("m1")
        self.assertEqual(detail["shadow"], runs)

    def test_a_match_with_no_panel_reports_an_empty_list(self):
        past = isoformat(utc_now() - timedelta(days=1))
        self.db.add_match(Match("bare", "Alpha", "Bravo", 3, 0.5, scheduled_at=past))
        self.assertEqual(self.db.shadow_panel_for_match("bare"), [])
        self.assertEqual(self.db.match_detail("bare")["shadow"], [])

    def test_members_are_never_attached_to_another_run(self):
        # Two runs on one match happen when the model changes mid-fixture.
        self._scored_run(
            "m1", 0.49, 0.425, "A", liquidity=31000.0,
            members=(0.48, 0.48, 0.50, 0.50), model="deepseek-v4-pro",
        )
        run_id = self.db.begin_shadow_panel_run(
            match_id="m1", evidence_cutoff_at=isoformat(utc_now()),
            panel_version=PANEL_VERSION, provider="deepseek",
            model="deepseek-v4-flash", backend="deepseek",
            grounded_teams=2, liquidity=31000.0,
        )
        self.db.record_shadow_panel_member(
            run_id=run_id, role=PANEL_ROLES[0].name, prompt_sha256="%064d" % 9,
            parsed={
                "probability_team_a": 0.53, "confidence": "low",
                "reasoning_summary": "r", "key_factors": [],
                "supporting_evidence": [], "assumptions": [],
                "usage": {}, "raw_response": "{}",
            },
        )
        self.db.finish_shadow_panel_run(
            run_id=run_id, status="partial",
            consensus={"probability_a": 0.53, "uncertainty_low_a": 0.48,
                       "uncertainty_high_a": 0.58, "spread": 0.0, "mad": 0.0},
            market_probability_a=0.405, market_captured_at=isoformat(utc_now()),
            usage={}, errors=[],
        )
        runs = {run["model"]: run for run in self.db.shadow_panel_for_match("m1")}
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(runs["deepseek-v4-pro"]["members"]), 4)
        self.assertEqual(len(runs["deepseek-v4-flash"]["members"]), 1)
        self.assertAlmostEqual(
            runs["deepseek-v4-flash"]["members"][0]["probability_a"], 0.53
        )

    def test_liquidity_is_captured_at_run_time_not_read_back_later(self):
        self._scored_run("m1", 0.6, 0.5, "A", liquidity=31000.0)
        with self.db.connect() as connection:
            stored = connection.execute(
                "SELECT liquidity_at_run FROM shadow_panel_runs WHERE run_id=1"
            ).fetchone()[0]
            # The match's own liquidity moves after the panel ran; the run row
            # must keep the depth the decision was actually made against.
            connection.execute("UPDATE matches SET liquidity=100 WHERE match_id='m1'")
        self.assertEqual(stored, 31000.0)
        cohort = shadow_score(self.db)["cohorts"][0]
        self.assertEqual(cohort["strata"][0]["band"], "tradeable")

    def test_unresolved_runs_are_counted_but_never_scored(self):
        self._scored_run("m1", 0.6, 0.5, "A", liquidity=30000.0, resolve=False)
        report = shadow_score(self.db)
        self.assertEqual(report["runs_total"], 1)
        self.assertEqual(report["runs_scored"], 0)
        self.assertEqual(report["runs_awaiting_result"], 1)
        self.assertEqual(report["cohorts"], [])

    def test_a_short_cohort_reports_no_winner(self):
        self._scored_run("m1", 0.9, 0.5, "A", liquidity=30000.0)
        self._scored_run("m2", 0.9, 0.5, "A", liquidity=30000.0)
        cohort = shadow_score(self.db)["cohorts"][0]
        self.assertLess(cohort["panel"]["brier"], cohort["market"]["brier"])
        self.assertEqual(cohort["verdict"], "too close to call")
        self.assertFalse(cohort["comparison"]["significant"])

    def test_thin_and_tradeable_books_are_scored_separately(self):
        self._scored_run("deep", 0.6, 0.5, "A", liquidity=TRADEABLE_LIQUIDITY + 1)
        self._scored_run("thin", 0.6, 0.5, "A", liquidity=TRADEABLE_LIQUIDITY - 1)
        strata = {s["band"]: s for s in shadow_score(self.db)["cohorts"][0]["strata"]}
        self.assertEqual(strata["tradeable"]["n"], 1)
        self.assertEqual(strata["thin"]["n"], 1)

    def test_each_model_is_its_own_cohort(self):
        self._scored_run("m1", 0.6, 0.5, "A", liquidity=30000.0, model="pro")
        self._scored_run("m1b", 0.7, 0.5, "A", liquidity=30000.0, model="flash")
        models = {c["model"] for c in shadow_score(self.db)["cohorts"]}
        self.assertEqual(models, {"pro", "flash"})

    def test_every_member_is_scored_against_the_median_it_fed(self):
        # base-rate is wildly wrong; the median must look better than it.
        self._scored_run(
            "m1", 0.6, 0.5, "A", liquidity=30000.0,
            members=(0.62, 0.58, 0.05, 0.60),
        )
        roles = {r["role"]: r for r in shadow_score(self.db)["cohorts"][0]["roles"]}
        self.assertEqual(len(roles), 4)
        self.assertGreater(roles["base-rate"]["brier"], roles["team-a-case"]["brier"])
        # The single-vs-median question must be answerable per role.
        self.assertIn("vs_panel", roles["base-rate"])
