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

from .gamma import GammaClient, final_map_score, resolution_from_event
from .paper import settle_match
from .storage import Database
from .timeutil import isoformat, parse_timestamp, utc_now
from .types import LiveState


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


def _record_terminal_state(
    database: Database, row: Dict[str, Any], event: Dict[str, Any]
) -> None:
    """Persist the final map score without manufacturing a terminal forecast."""
    maps = final_map_score(event, row["team_a"], row["team_b"])
    if maps is None:
        return

    try:
        latest = database.latest_state(row["match_id"])
    except KeyError:
        latest = None
    if (
        latest is not None
        and latest.source == "polymarket-gamma-final"
        and latest.maps_a == maps["maps_a"]
        and latest.maps_b == maps["maps_b"]
    ):
        return

    observed = isoformat(utc_now())
    database.record_state(
        LiveState(
            match_id=row["match_id"],
            source_at=observed,
            observed_at=observed,
            maps_a=maps["maps_a"],
            maps_b=maps["maps_b"],
            rounds_a=0,
            rounds_b=0,
            current_map="FINAL",
            source="polymarket-gamma-final",
            raw={
                "event_id": event.get("id"),
                "period": event.get("period"),
                "score": event.get("score"),
            },
        ).normalized()
    )


def _resolve_event(
    database: Database,
    row: Dict[str, Any],
    event: Optional[Dict[str, Any]],
    account_name: str,
    result: ResolutionResult,
) -> None:
    if event is None:
        result.pending += 1
        return

    # Fixture completion and market settlement are separate events. Record the
    # final maps as soon as Gamma knows them, but never create a forecast from a
    # terminal snapshot: that would leak the result into the paper strategy.
    if event.get("ended"):
        database.update_match_lifecycle(row["match_id"], live=False, ended=True)
        _record_terminal_state(database, row, event)

    decision = resolution_from_event(event, row["team_a"], row["team_b"])
    if decision is None:
        result.pending += 1
        return

    resolved_at = isoformat(utc_now())
    if decision.get("void"):
        database.void_match(row["match_id"], resolved_at)
        result.voided += 1
        return

    winner = decision["winner"]
    database.resolve_match(row["match_id"], winner, resolved_at)
    result.resolved += 1

    # Positions can only exist where a forecast exists, so a missing forecast
    # id simply means there is nothing to pay out.
    try:
        forecast_id = database.latest_forecast_id(row["match_id"])
    except (KeyError, ValueError):
        forecast_id = None
    if forecast_id is not None:
        # A new execution methodology starts in a new account, but older
        # accounts may still carry positions from this match. Resolution is a
        # terminal event for all of them; settling only the collector's active
        # account would strand legacy cash forever.
        accounts = database.paper_accounts_for_match(row["match_id"])
        for settlement_account in accounts:
            actions = settle_match(
                database=database,
                account_name=settlement_account,
                match_id=row["match_id"],
                winner=winner,
                forecast_id=forecast_id,
            )
            result.settled_trades += len(actions)

    result.decided.append(
        {
            "match_id": row["match_id"],
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "winner": winner,
            "winning_team": row["team_a"] if winner == "A" else row["team_b"],
        }
    )


def resolve_known_events(
    database: Database,
    events: Dict[str, Dict[str, Any]],
    account_name: str = "live-paper",
) -> ResolutionResult:
    """Immediately process ended events already returned by discovery.

    This costs no extra Gamma requests and closes a match in the same collector
    cycle when the moneyline has already reached an unambiguous 1/0 result.
    """
    result = ResolutionResult()
    if not events:
        return result
    database.initialize()
    rows = {
        row["match_id"]: row
        for row in database.open_matches()
        if row["match_id"] in events
    }
    for match_id, event in events.items():
        row = rows.get(match_id)
        if row is None:
            continue
        result.checked += 1
        try:
            _resolve_event(database, row, event, account_name, result)
        except Exception as error:
            result.errors.append("%s: %s" % (match_id, error))
    return result


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
            _resolve_event(database, row, client.get_event(match_id), account_name, result)
        except Exception as error:
            result.errors.append("%s: %s" % (match_id, error))

    return result
