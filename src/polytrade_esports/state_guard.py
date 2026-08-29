"""Canonical live-state transition guard.

Providers are allowed to repeat snapshots and to advance to a new map, but a
same-map score may never move backwards.  When round detail disappears we keep
the last trusted round state for forecasting while the collector separately
disables new entries.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .types import LiveState


FROZEN_SOURCE = "canonical-frozen"
STRATEGIES = ("pre-match", "map-boundary", "round-live")


@dataclass(frozen=True)
class StateDecision:
    state: LiveState
    accepted: bool
    frozen: bool = False
    reason: str = ""


def _map_number(value: str) -> Optional[int]:
    match = re.search(r"(?:map\s*)?(\d+)", str(value or ""), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _map_advanced(previous: LiveState, candidate: LiveState) -> bool:
    if candidate.maps_a + candidate.maps_b > previous.maps_a + previous.maps_b:
        return True
    old_number = _map_number(previous.current_map)
    new_number = _map_number(candidate.current_map)
    # Provider pre-match periods such as ``0/3`` mean "map one has not
    # completed", not "current map zero". Do not label the first real round
    # snapshot as a map-boundary transition merely because it says Map 1.
    return (
        old_number is not None
        and old_number >= 1
        and new_number is not None
        and new_number > old_number
    )


def _frozen_state(
    previous: LiveState,
    candidate: LiveState,
    reason: str,
    rejected: bool,
) -> LiveState:
    raw: Dict[str, Any] = dict(candidate.raw or {})
    raw["canonical_guard"] = {
        "frozen": True,
        "rejected": rejected,
        "reason": reason,
        "candidate": {
            "maps_a": candidate.maps_a,
            "maps_b": candidate.maps_b,
            "rounds_a": candidate.rounds_a,
            "rounds_b": candidate.rounds_b,
            "current_map": candidate.current_map,
            "source": candidate.source,
        },
        "trusted_source": previous.source,
        "trusted_source_at": previous.source_at,
    }
    return LiveState(
        match_id=candidate.match_id,
        source_at=candidate.source_at,
        observed_at=candidate.observed_at,
        maps_a=previous.maps_a,
        maps_b=previous.maps_b,
        rounds_a=previous.rounds_a,
        rounds_b=previous.rounds_b,
        current_map=previous.current_map,
        side_advantage_a=previous.side_advantage_a,
        economy_a=previous.economy_a,
        economy_b=previous.economy_b,
        map_bias_a=previous.map_bias_a,
        source=FROZEN_SOURCE if rejected else candidate.source,
        raw=raw,
    ).normalized()


def canonicalize_state(
    previous: Optional[LiveState],
    candidate: LiveState,
    round_detail_available: bool,
) -> StateDecision:
    """Accept a valid transition or freeze the last trusted state.

    A map-score increment (or an explicit later map period) is the only event
    that permits rounds to reset. Missing round detail is a degraded but valid
    observation, so it freezes without being counted as provider corruption.
    """
    current = candidate.normalized()
    if previous is None:
        return StateDecision(current, accepted=True)
    trusted = previous.normalized()
    if trusted.match_id != current.match_id:
        raise ValueError("state transition must reference one match")

    if current.maps_a < trusted.maps_a or current.maps_b < trusted.maps_b:
        reason = "map_score_regressed"
        return StateDecision(
            _frozen_state(trusted, current, reason, rejected=True),
            accepted=False,
            frozen=True,
            reason=reason,
        )

    old_map = _map_number(trusted.current_map)
    new_map = _map_number(current.current_map)
    if (
        old_map is not None
        and new_map is not None
        and new_map < old_map
        and current.maps_a + current.maps_b == trusted.maps_a + trusted.maps_b
    ):
        reason = "current_map_regressed"
        return StateDecision(
            _frozen_state(trusted, current, reason, rejected=True),
            accepted=False,
            frozen=True,
            reason=reason,
        )

    advanced = _map_advanced(trusted, current)
    if not advanced and round_detail_available:
        if current.rounds_a < trusted.rounds_a or current.rounds_b < trusted.rounds_b:
            reason = "same_map_round_score_regressed"
            return StateDecision(
                _frozen_state(trusted, current, reason, rejected=True),
                accepted=False,
                frozen=True,
                reason=reason,
            )

    if not advanced and not round_detail_available:
        reason = "round_detail_unavailable"
        return StateDecision(
            _frozen_state(trusted, current, reason, rejected=False),
            accepted=True,
            frozen=True,
            reason=reason,
        )

    return StateDecision(current, accepted=True)


def strategy_for_state(
    live: bool,
    previous: Optional[LiveState],
    current: LiveState,
    round_detail_available: bool,
) -> str:
    """Name the information horizon responsible for this decision."""
    if not live:
        return "pre-match"
    if previous is not None and _map_advanced(previous, current):
        return "map-boundary"
    if not round_detail_available:
        return "map-boundary"
    return "round-live"


def validate_strategy(value: str) -> str:
    strategy = str(value or "").strip().lower()
    if strategy not in STRATEGIES:
        raise ValueError("strategy must be one of: %s" % ", ".join(STRATEGIES))
    return strategy
