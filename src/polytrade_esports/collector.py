"""Live collection cycle: discover, read state, read books, forecast.

One cycle is deliberately fault-isolated per match. A single unreachable order
book or a provider hiccup must not abort the whole sweep, because the sweep is
what keeps every other live match current.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .engine import tick
from .gamma import GammaClient, is_stale, parse_event
from .pandascore import PandaScoreClient, PandaScoreError, build_state, team_a_index
from .paper import PaperConfig
from .polymarket import PolymarketBookClient
from .resolver import resolve_open_matches
from .storage import Database
from .timeutil import isoformat, parse_timestamp, utc_now
from .types import LiveState

# Neutral starting prior. Every match is inserted at even odds and only moves
# when an LLM prior is applied, so an un-forecast match can never masquerade as
# a confident one.
SEED_PRIOR_A = 0.5
MAPS_ONLY_NOTICE = (
    "round-level PandaScore data is unavailable; model updates are maps-only"
)

# Shared across cycles so a refusal is remembered between polls.
_GAME_DETAIL_GATE = None


@dataclass
class CollectorConfig:
    account_name: str = "live-paper"
    tick_window_hours: float = 3.0
    min_liquidity: float = 0.0
    max_pages: int = 6
    paper: Optional[PaperConfig] = None
    pandascore_token: str = ""
    # Polymarket leaves finished esports events open for days while UMA
    # settles, so an unfiltered sweep accumulates months of dead fixtures.
    max_match_age_hours: float = 12.0
    # Settlement takes 6-24h, so a 10-minute re-check was 60x more often than
    # the underlying state can change. Half-hourly is still far ahead of it.
    resolve_every_cycles: int = 30


class GameDetailGate:
    """Stops re-requesting per-map detail once the plan has refused it.

    ``/csgo/games/{id}`` is plan-gated. Without this latch every live match
    re-requests it on every cycle, spending free-tier quota on a guaranteed 403
    and filling the error list with the same message. One 403 closes the gate
    for the life of the process; a restart re-checks, so an upgraded plan is
    picked up without a code change.
    """

    def __init__(self) -> None:
        self.open = True
        self.reason = ""

    def close(self, reason: str) -> None:
        self.open = False
        self.reason = reason


@dataclass
class CycleResult:
    discovered: int = 0
    inserted: int = 0
    conflicts: int = 0
    ticked: int = 0
    skipped: int = 0
    stale: int = 0
    finished: int = 0
    resolved: int = 0
    errors: List[str] = field(default_factory=list)
    # Expected limitations (a plan that withholds an endpoint, say) are worth
    # surfacing but are not failures, so they must not degrade the run status.
    notices: List[str] = field(default_factory=list)
    forecasts: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "discovered": self.discovered,
            "inserted": self.inserted,
            "conflicts": self.conflicts,
            "ticked": self.ticked,
            "skipped": self.skipped,
            "stale": self.stale,
            "finished": self.finished,
            "resolved": self.resolved,
            "errors": self.errors,
            "notices": self.notices,
            "forecasts": self.forecasts,
        }


def _should_tick(row: Dict[str, Any], window_hours: float) -> bool:
    if row.get("live"):
        return True
    scheduled = row.get("scheduled_at")
    if not scheduled:
        return False
    try:
        starts_at = parse_timestamp(scheduled)
    except ValueError:
        return False
    delta_hours = (starts_at - utc_now()).total_seconds() / 3600.0
    # Track a match from a few hours out (book building) until well past the
    # scheduled start, since esports schedules slip routinely.
    return -6.0 <= delta_hours <= window_hours


def _fallback_state(match_id: str, record: Dict[str, Any]) -> LiveState:
    """Maps-only state derived from resolved per-map markets.

    Used when no live provider is configured. It moves at map boundaries and is
    flat within a map, so ``rounds`` stay at 0 and the engine falls back to the
    map-level term alone.
    """
    now = isoformat(utc_now())
    maps = record.get("map_score") or {"maps_a": 0, "maps_b": 0}
    return LiveState(
        match_id=match_id,
        source_at=now,
        observed_at=now,
        maps_a=int(maps.get("maps_a", 0)),
        maps_b=int(maps.get("maps_b", 0)),
        rounds_a=0,
        rounds_b=0,
        current_map=record.get("period") or "unknown",
        source="polymarket-gamma-maps",
        raw={"period": record.get("period"), "score": record.get("score")},
    ).normalized()


def run_cycle(
    database: Database,
    config: Optional[CollectorConfig] = None,
    gamma: Optional[GammaClient] = None,
    books: Optional[PolymarketBookClient] = None,
    cycle_index: int = 0,
) -> CycleResult:
    global _GAME_DETAIL_GATE
    if _GAME_DETAIL_GATE is None:
        _GAME_DETAIL_GATE = GameDetailGate()
    gate = _GAME_DETAIL_GATE
    settings = config or CollectorConfig()
    gamma_client = gamma or GammaClient()
    book_client = books or PolymarketBookClient()
    result = CycleResult()
    database.initialize()
    database.ensure_account(settings.account_name)
    run_id = database.start_collector_run()

    records: Dict[str, Dict[str, Any]] = {}
    try:
        for event in gamma_client.cs2_events(max_pages=settings.max_pages):
            record = parse_event(event)
            if record is None or not record["match_id"]:
                continue
            records[record["match_id"]] = record
            if is_stale(record, settings.max_match_age_hours):
                # Terminal events are excluded from discovery, but their state
                # must still be persisted. Otherwise a row that was once live
                # remains live forever while settlement is pending.
                if record.get("ended") and database.update_match_lifecycle(
                    record["match_id"], live=False, ended=True
                ):
                    result.finished += 1
                result.stale += 1
                continue
            outcome = database.upsert_discovered_match(record, SEED_PRIOR_A)
            result.discovered += 1
            if outcome == "inserted":
                result.inserted += 1
            elif outcome == "conflict":
                result.conflicts += 1
                result.errors.append(
                    "%s: identity changed on an existing slug; skipped" % record["match_id"]
                )
    except Exception as error:  # network/schema failure: report, keep going
        result.errors.append("discovery failed: %s" % error)

    # Closed fixtures can disappear from the paginated discovery feed before
    # their previous live flag has been cleared. Reconcile only those rows that
    # the database still believes are live, so the extra request cost is bounded
    # and falls back to zero in steady state.
    for row in database.open_matches(only_live=True):
        match_id = row["match_id"]
        if match_id in records:
            continue
        try:
            event = gamma_client.get_event(match_id)
            record = parse_event(event) if event is not None else None
            if record is None:
                continue
            records[match_id] = record
            became_finished = database.update_match_lifecycle(
                match_id, live=bool(record.get("live")), ended=bool(record.get("ended"))
            )
            if became_finished:
                result.finished += 1
        except Exception as error:
            result.notices.append("%s: lifecycle refresh unavailable: %s" % (match_id, error))

    live_by_provider: Dict[str, Dict[str, Any]] = {}
    panda: Optional[PandaScoreClient] = None
    if settings.pandascore_token:
        try:
            panda = PandaScoreClient(settings.pandascore_token)
            for match in panda.running_matches():
                provider_id = str(match.get("id") or "")
                if provider_id:
                    live_by_provider[provider_id] = match
        except (PandaScoreError, ValueError) as error:
            result.errors.append("pandascore running_matches failed: %s" % error)
            panda = None

    for row in database.open_matches():
        match_id = row["match_id"]
        record = records.get(match_id)
        if record is None:
            result.skipped += 1
            continue
        if record.get("ended"):
            result.skipped += 1
            continue
        if not _should_tick(row, settings.tick_window_hours):
            result.skipped += 1
            continue
        if float(row.get("liquidity") or 0.0) < settings.min_liquidity:
            result.skipped += 1
            continue
        try:
            state = _resolve_state(
                database, panda, live_by_provider, row, record, result, gate
            )
            quote = book_client.get_pair(match_id, row["token_a"], row["token_b"])
            # A seed prior is the absence of a view, not a 50/50 view. Record
            # the forecast for the dashboard, but do not size a position from it.
            has_prior = str(row.get("prior_source") or "seed") != "seed"
            outcome = tick(
                database=database,
                state=state,
                quote=quote,
                account_name=settings.account_name,
                paper_config=settings.paper,
                paper_enabled=has_prior,
            )
            result.ticked += 1
            result.forecasts.append(
                {
                    "match_id": match_id,
                    "team_a": outcome["team_a"],
                    "team_b": outcome["team_b"],
                    "probability_a": outcome["probability_a"],
                    "edge_a": outcome["edge_a"],
                    "edge_b": outcome["edge_b"],
                    "best_side": outcome["best_side"],
                    "paper_enabled": outcome["paper_enabled"],
                }
            )
        except Exception as error:
            result.skipped += 1
            result.errors.append("%s: %s" % (match_id, error))

    # Resolution is periodic rather than per-cycle: it costs one Gamma request
    # per candidate and finished matches do not change minute to minute.
    if settings.resolve_every_cycles > 0 and cycle_index % settings.resolve_every_cycles == 0:
        try:
            settlement = resolve_open_matches(
                database=database,
                gamma=gamma_client,
                account_name=settings.account_name,
            )
            result.resolved = settlement.resolved + settlement.voided
            result.errors.extend(settlement.errors[:5])
        except Exception as error:
            result.errors.append("resolution failed: %s" % error)

    status = "completed" if not result.errors else ("partial" if result.ticked else "failed")
    persisted_notices = list(result.notices)
    if not settings.pandascore_token or not gate.open:
        persisted_notices.append(MAPS_ONLY_NOTICE)
    # Preserve order while removing the first-cycle notice duplicate.
    persisted_notices = list(dict.fromkeys(persisted_notices))
    database.finish_collector_run(
        run_id=run_id,
        status=status,
        discovered=result.discovered,
        ticked=result.ticked,
        skipped=result.skipped,
        errors=result.errors,
        notices=persisted_notices,
    )
    return result


def _resolve_state(
    database: Database,
    panda: Optional[PandaScoreClient],
    live_by_provider: Dict[str, Dict[str, Any]],
    row: Dict[str, Any],
    record: Dict[str, Any],
    result: CycleResult,
    gate: "GameDetailGate",
) -> LiveState:
    """Prefer real provider state; degrade to map-market state, never to nothing."""
    provider_id = str(row.get("provider_match_id") or "")
    match = live_by_provider.get(provider_id)
    if panda is not None and match is not None:
        try:
            index = team_a_index(match, row["team_a"])
            game_detail = None
            for game in match.get("games") or []:
                if isinstance(game, dict) and game.get("status") == "running":
                    if gate.open:
                        try:
                            game_detail = panda.game(game.get("id"))
                        except PandaScoreError as error:
                            # Round detail is plan-gated; maps-level state still
                            # works, so close the gate and carry on quietly.
                            if error.status in (401, 403):
                                gate.close(str(error))
                                result.notices.append(MAPS_ONLY_NOTICE)
                            else:
                                result.errors.append(
                                    "%s: pandascore game detail unavailable: %s"
                                    % (row["match_id"], error)
                                )
                    break
            state = build_state(
                match_id=row["match_id"],
                match=match,
                team_a_index=index,
                game_detail=game_detail,
            )
            if state is not None:
                return state
        except (PandaScoreError, ValueError) as error:
            result.errors.append("%s: pandascore state failed: %s" % (row["match_id"], error))
    return _fallback_state(row["match_id"], record)


def run_loop(
    database: Database,
    config: Optional[CollectorConfig] = None,
    interval_seconds: float = 60.0,
    cycles: int = 0,
    on_cycle: Optional[Any] = None,
) -> None:
    """Run cycles forever (``cycles=0``) or a fixed number, sleeping between."""
    completed = 0
    while cycles <= 0 or completed < cycles:
        started = time.time()
        result = run_cycle(database, config, cycle_index=completed)
        if on_cycle is not None:
            on_cycle(result)
        completed += 1
        if cycles > 0 and completed >= cycles:
            return
        elapsed = time.time() - started
        remaining = max(1.0, float(interval_seconds) - elapsed)
        time.sleep(remaining)
