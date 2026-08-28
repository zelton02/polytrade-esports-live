from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from .timeutil import canonical_timestamp, isoformat, parse_timestamp, utc_now


def _probability(name: str, value: float) -> float:
    result = float(value)
    if not 0.0 < result < 1.0:
        raise ValueError("%s must be strictly between 0 and 1" % name)
    return result


def _bounded(name: str, value: float) -> float:
    result = float(value)
    if not -1.0 <= result <= 1.0:
        raise ValueError("%s must be between -1 and 1" % name)
    return result


@dataclass(frozen=True)
class Match:
    match_id: str
    team_a: str
    team_b: str
    best_of: int
    prior_probability_a: float
    token_a: str = ""
    token_b: str = ""
    source: str = "manual"
    external_id: str = ""
    scheduled_at: Optional[str] = None

    def validated(self) -> "Match":
        if not self.match_id.strip():
            raise ValueError("match_id is required")
        if not self.team_a.strip() or not self.team_b.strip():
            raise ValueError("both team names are required")
        if self.team_a.strip() == self.team_b.strip():
            raise ValueError("team names must differ")
        if self.best_of not in (1, 3, 5):
            raise ValueError("best_of must be 1, 3, or 5")
        _probability("prior_probability_a", self.prior_probability_a)
        if self.scheduled_at:
            canonical_timestamp(self.scheduled_at)
        return self


@dataclass(frozen=True)
class LiveState:
    match_id: str
    source_at: str
    maps_a: int
    maps_b: int
    rounds_a: int
    rounds_b: int
    current_map: str = "unknown"
    side_advantage_a: float = 0.0
    economy_a: float = 0.0
    economy_b: float = 0.0
    map_bias_a: float = 0.0
    source: str = "manual"
    observed_at: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None

    def normalized(self) -> "LiveState":
        if self.maps_a < 0 or self.maps_b < 0:
            raise ValueError("map scores cannot be negative")
        if self.rounds_a < 0 or self.rounds_b < 0:
            raise ValueError("round scores cannot be negative")
        _bounded("side_advantage_a", self.side_advantage_a)
        _bounded("economy_a", self.economy_a)
        _bounded("economy_b", self.economy_b)
        _bounded("map_bias_a", self.map_bias_a)
        source_at = canonical_timestamp(self.source_at)
        observed_at = (
            canonical_timestamp(self.observed_at)
            if self.observed_at
            else isoformat(utc_now())
        )
        if parse_timestamp(source_at) > parse_timestamp(observed_at):
            raise ValueError("source_at cannot be later than observed_at")
        if parse_timestamp(observed_at) > utc_now() + timedelta(seconds=5):
            raise ValueError("observed_at cannot be in the future")
        return LiveState(
            match_id=self.match_id,
            source_at=source_at,
            maps_a=int(self.maps_a),
            maps_b=int(self.maps_b),
            rounds_a=int(self.rounds_a),
            rounds_b=int(self.rounds_b),
            current_map=self.current_map or "unknown",
            side_advantage_a=float(self.side_advantage_a),
            economy_a=float(self.economy_a),
            economy_b=float(self.economy_b),
            map_bias_a=float(self.map_bias_a),
            source=self.source or "manual",
            observed_at=observed_at,
            raw=dict(self.raw or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BookQuote:
    match_id: str
    bid_a: float
    ask_a: float
    bid_b: float
    ask_b: float
    source_at: str
    observed_at: Optional[str] = None
    source: str = "manual"
    raw: Optional[Dict[str, Any]] = None

    def normalized(self) -> "BookQuote":
        for name in ("bid_a", "ask_a", "bid_b", "ask_b"):
            _probability(name, getattr(self, name))
        if self.bid_a > self.ask_a or self.bid_b > self.ask_b:
            raise ValueError("book bid cannot exceed ask")
        source_at = canonical_timestamp(self.source_at)
        observed_at = (
            canonical_timestamp(self.observed_at)
            if self.observed_at
            else isoformat(utc_now())
        )
        if parse_timestamp(source_at) > parse_timestamp(observed_at):
            raise ValueError("book source_at cannot be later than observed_at")
        if parse_timestamp(observed_at) > utc_now() + timedelta(seconds=5):
            raise ValueError("book observed_at cannot be in the future")
        return BookQuote(
            match_id=self.match_id,
            bid_a=float(self.bid_a),
            ask_a=float(self.ask_a),
            bid_b=float(self.bid_b),
            ask_b=float(self.ask_b),
            source_at=source_at,
            observed_at=observed_at,
            source=self.source or "manual",
            raw=dict(self.raw or {}),
        )

    @property
    def midpoint_a(self) -> float:
        return (self.bid_a + self.ask_a) / 2.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
