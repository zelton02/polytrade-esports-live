"""Batch runner for LLM pre-match priors, with cost guards.

Selection rule: only open, not-yet-live matches that still carry the neutral
seed prior, most liquid first. Liquidity is the ranking signal because an
illiquid market cannot absorb a paper position anyway, so spending budget on it
buys nothing.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .liquipedia import LiquipediaClient, LiquipediaError
from .llm import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    ForecastBackendError,
    add_usage,
    build_prior_prompt,
    forecast_prior,
    format_team_facts,
)
from .polymarket import PolymarketBookClient
from .storage import Database
from .timeutil import isoformat, parse_timestamp, utc_now


def gather_facts(
    database: Database,
    row: Dict[str, Any],
    client: Optional[LiquipediaClient],
    max_age_hours: float = 12.0,
) -> Tuple[str, int]:
    """Fetch and cache Liquipedia facts for both teams, as a prompt block.

    Results at or after the match start are excluded. Liquipedia publishes a
    fixture's result as soon as it is played, so an unfiltered fetch can put
    the answer inside the evidence for the question -- seen in testing, where
    the forecast match appeared in its own evidence block.

    A missing page is not an error worth failing the forecast over: lower-tier
    rosters often have no wiki entry, and the prompt states plainly when data
    could not be retrieved rather than leaving the model to fill the gap.
    """
    cutoff = None
    scheduled = row.get("scheduled_at")
    if scheduled:
        try:
            cutoff = parse_timestamp(scheduled).timestamp()
        except ValueError:
            cutoff = None
    if cutoff is None:
        cutoff = utc_now().timestamp()

    blocks = []
    grounded = 0
    for team in (row["team_a"], row["team_b"]):
        facts = database.cached_team_facts(team, max_age_hours=max_age_hours)
        if facts is None and client is not None:
            try:
                facts = client.team_facts(team, before_timestamp=cutoff)
            except LiquipediaError as error:
                # Budget exhaustion is transient; do not poison the cache with
                # it, or the team stays factless for the whole cache window.
                facts = {"page": None, "error": str(error)}
                if "budget" not in str(error):
                    database.store_team_facts(team, facts)
            else:
                database.store_team_facts(team, facts)
        if facts and not facts.get("error") and (facts.get("roster") or facts.get("recent")):
            grounded += 1
        blocks.append(format_team_facts(team, facts))
    return "\n\n".join(blocks), grounded


def _record_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "match_id": row["match_id"],
        "team_a": row["team_a"],
        "team_b": row["team_b"],
        "best_of": row["best_of"],
        "league": row.get("league", ""),
        "serie": row.get("serie", ""),
        "tournament": row.get("tournament", ""),
        "context": row.get("context", ""),
        "scheduled_at": row.get("scheduled_at"),
    }


def run_priors(
    database: Database,
    backend: Any,
    limit: int = 5,
    daily_limit: int = 40,
    monthly_budget_usd: float = 6.0,
    max_cost_per_forecast: float = 0.10,
    min_liquidity: float = 0.0,
    provider: str = DEFAULT_PROVIDER,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    books: Optional[PolymarketBookClient] = None,
    backend_name: str = "",
    liquipedia: Optional[LiquipediaClient] = None,
    use_liquipedia: bool = True,
    require_facts: bool = True,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    if limit <= 0 or daily_limit <= 0:
        raise ValueError("prior limits must be positive")
    if monthly_budget_usd < 0 or max_cost_per_forecast <= 0:
        raise ValueError("prior cost limits are invalid")

    database.initialize()
    now = utc_now()
    day_start = isoformat(datetime(now.year, now.month, now.day, tzinfo=timezone.utc))
    month_start = isoformat(datetime(now.year, now.month, 1, tzinfo=timezone.utc))
    already_today = database.count_priors_since(day_start)
    month_cost = database.prior_cost_since(month_start)
    slots = max(0, min(limit, daily_limit - already_today))

    candidates = (
        database.matches_needing_prior(limit=slots, min_liquidity=min_liquidity)
        if slots > 0
        else []
    )

    book_client = books or PolymarketBookClient()
    facts_client = liquipedia or (LiquipediaClient() if use_liquipedia else None)
    created: List[Dict[str, Any]] = []
    errors: List[str] = []
    skipped = 0
    usage_total: Dict[str, Any] = {}

    if not dry_run:
        for index, row in enumerate(candidates):
            spent = month_cost + float(usage_total.get("estimated_cost_usd", 0.0))
            if spent >= monthly_budget_usd:
                skipped += len(candidates) - index
                errors.append(
                    "monthly budget %.2f USD reached; %d candidates skipped"
                    % (monthly_budget_usd, len(candidates) - index)
                )
                break
            record = _record_from_row(row)
            verified, grounded = gather_facts(database, row, facts_client)
            # No facts, no forecast. A prior with nothing to reason from is
            # the failure mode that invalidated the first cohort: it looks
            # like a view, unlocks the paper engine, and is really just a
            # paraphrase of whatever text happened to be in the prompt.
            if require_facts and grounded == 0:
                skipped += 1
                # Stamped so the queue moves on. Without it the same fixture is
                # reselected every cycle, and with a small batch size a handful
                # of teams that will never have a wiki page can starve every
                # match that could actually be priced.
                database.mark_prior_skipped(row["match_id"])
                errors.append(
                    "%s: no verified team facts; skipped rather than writing an "
                    "ungrounded prior" % row["match_id"]
                )
                continue
            record["verified_facts"] = verified
            cutoff = isoformat(utc_now())
            prompt_sha = hashlib.sha256(
                build_prior_prompt(record, cutoff).encode("utf-8")
            ).hexdigest()
            try:
                parsed = forecast_prior(backend, record, evidence_cutoff_at=cutoff)
            except ForecastBackendError as error:
                add_usage(usage_total, error.usage)
                errors.append("%s: %s" % (row["match_id"], error))
                continue
            except ValueError as error:
                errors.append("%s: invalid forecast: %s" % (row["match_id"], error))
                continue
            add_usage(usage_total, parsed.get("usage") or {})
            prior_id = database.apply_prior(
                match_id=row["match_id"],
                parsed=parsed,
                provider=provider,
                model=model,
                prompt_sha256=prompt_sha,
                backend=backend_name or provider,
                verified_facts=verified,
                grounded_teams=grounded,
            )
            # Record what the market thought at this instant. Scoring the AI
            # against a price sampled at some other time measures timing, not
            # skill, so the baseline has to be captured here or not at all.
            market_probability = None
            try:
                quote = book_client.get_pair(
                    row["match_id"], row["token_a"], row["token_b"]
                )
                market_probability = quote.midpoint_a
                database.set_prior_market_probability(prior_id, market_probability)
            except Exception as error:
                errors.append(
                    "%s: prior stored but market baseline unavailable: %s"
                    % (row["match_id"], error)
                )

            created.append(
                {
                    "prior_id": prior_id,
                    "market_probability_a": market_probability,
                    "match_id": row["match_id"],
                    "team_a": row["team_a"],
                    "team_b": row["team_b"],
                    "probability_a": parsed["probability_team_a"],
                    "confidence": parsed["confidence"],
                    "reasoning_summary": parsed["reasoning_summary"],
                    "grounded_teams": grounded,
                }
            )
            call_cost = float((parsed.get("usage") or {}).get("estimated_cost_usd", 0.0) or 0.0)
            if call_cost > max_cost_per_forecast:
                errors.append(
                    "%s: per-forecast cost cap exceeded (%.4f > %.4f); stopping"
                    % (row["match_id"], call_cost, max_cost_per_forecast)
                )
                skipped += len(candidates) - index - 1
                break
    else:
        created = [
            {
                "match_id": row["match_id"],
                "team_a": row["team_a"],
                "team_b": row["team_b"],
                "liquidity": row.get("liquidity"),
                "scheduled_at": row.get("scheduled_at"),
            }
            for row in candidates
        ]

    summary = {
        "dry_run": dry_run,
        "provider": provider,
        "backend": backend_name or provider,
        "model": model,
        "already_today": already_today,
        "daily_limit": daily_limit,
        "candidates_selected": len(candidates),
        "priors_created": 0 if dry_run else len(created),
        "skipped": skipped,
        "month_cost_before_run_usd": month_cost,
        "usage": usage_total,
        "errors": errors,
        "status": "completed" if not errors else ("partial" if created else "failed"),
    }
    return summary, created
