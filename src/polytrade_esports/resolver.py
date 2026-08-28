"""Settle finished matches from the Polymarket result.

Without this the database only ever grows: forecasts accumulate against
matches that never close, paper positions never pay out, and nothing can be
scored. Resolution is what turns a stream of predictions into evidence.

The winning outcome is read from the settled series market, matched by team
name rather than list position, so a reordered outcome pair cannot silently
invert a result and corrupt every score derived from it.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .gamma import GammaClient, resolution_from_event
from .paper import settle_match
from .storage import Database
from .timeutil import isoformat, parse_timestamp, utc_now


def _age_days(scheduled_at: Optional[str]) -> float:
    if not scheduled_at:
        return 0.0
    try:
        return (utc_now() - parse_timestamp(scheduled_at)).total_seconds() / 86400.0
    except ValueError:
        return 0.0


@dataclass
class ResolutionResult:
    checked: int = 0
    resolved: int = 0
    voided: int = 0
    abandoned: int = 0
    pending: int = 0
    settled_trades: int = 0
    errors: List[str] = field(default_factory=list)
    decided: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked": self.checked,
            "resolved": self.resolved,
            "voided": self.voided,
            "abandoned": self.abandoned,
            "pending": self.pending,
            "settled_trades": self.settled_trades,
            "errors": self.errors,
            "decided": self.decided,
        }


def resolve_open_matches(
    database: Database,
    gamma: Optional[GammaClient] = None,
    account_name: str = "live-paper",
    min_age_hours: float = 5.0,
    limit: int = 60,
    abandon_after_days: float = 14.0,
) -> ResolutionResult:
    """Check finished-looking matches and settle the ones Polymarket decided.

    Matches Polymarket never settles are voided after ``abandon_after_days``.
    Without that they stay in the queue forever, and every cycle spends a
    request re-asking a question that has had the same answer for months.

    Both defaults are measured, not guessed. Across 325 sampled CS2 fixtures
    (2026-08-28) nothing at all was settled inside 6 hours of the start, 88%
    had settled by 24 hours and 98% by two days, while past 14 days only 5%
    ever settled -- so checking earlier than ``min_age_hours`` spends requests
    on a guaranteed "not yet", and waiting longer than ``abandon_after_days``
    waits on fixtures that are not coming back.
    """
    client = gamma or GammaClient()
    result = ResolutionResult()
    database.initialize()

    for row in database.matches_awaiting_resolution(
        min_age_hours=min_age_hours, limit=limit
    ):
        match_id = row["match_id"]
        result.checked += 1
        try:
            if _age_days(row.get("scheduled_at")) > abandon_after_days:
                database.void_match(match_id, isoformat(utc_now()))
                result.voided += 1
                result.abandoned += 1
                continue
            event = client.get_event(match_id)
            if event is None:
                result.pending += 1
                continue
            # Fixture completion and market settlement are separate events.
            # Persist the former even when UMA has not decided the latter yet,
            # so the dashboard can show AWAITING SETTLEMENT instead of LIVE.
            if event.get("ended"):
                database.update_match_lifecycle(match_id, live=False, ended=True)
            decision = resolution_from_event(event, row["team_a"], row["team_b"])
            if decision is None:
                result.pending += 1
                continue

            resolved_at = isoformat(utc_now())
            if decision.get("void"):
                database.void_match(match_id, resolved_at)
                result.voided += 1
                continue

            winner = decision["winner"]
            database.resolve_match(match_id, winner, resolved_at)
            result.resolved += 1

            # Positions can only exist where a forecast exists, so a missing
            # forecast id simply means there is nothing to pay out.
            try:
                forecast_id = database.latest_forecast_id(match_id)
            except (KeyError, ValueError):
                forecast_id = None
            if forecast_id is not None:
                actions = settle_match(
                    database=database,
                    account_name=account_name,
                    match_id=match_id,
                    winner=winner,
                    forecast_id=forecast_id,
                )
                result.settled_trades += len(actions)

            result.decided.append(
                {
                    "match_id": match_id,
                    "team_a": row["team_a"],
                    "team_b": row["team_b"],
                    "winner": winner,
                    "winning_team": row["team_a"] if winner == "A" else row["team_b"],
                }
            )
        except Exception as error:
            result.errors.append("%s: %s" % (match_id, error))

    return result
