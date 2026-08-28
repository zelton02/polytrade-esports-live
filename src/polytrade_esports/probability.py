import math
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Dict

from .types import LiveState, Match


MODEL_VERSION = "cs2-state-v0.1"
EPSILON = 1e-6


def clamp_probability(value: float) -> float:
    return max(EPSILON, min(1.0 - EPSILON, float(value)))


def logit(probability: float) -> float:
    p = clamp_probability(probability)
    return math.log(p / (1.0 - p))


def logistic(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


@lru_cache(maxsize=8192)
def _map_from_score(rounds_a: int, rounds_b: int, rounded_round_p: int) -> float:
    p = rounded_round_p / 1_000_000.0
    a = int(rounds_a)
    b = int(rounds_b)

    if a >= 13 and a - b >= 2:
        return 1.0
    if b >= 13 and b - a >= 2:
        return 0.0

    if a >= 12 and b >= 12:
        deuce = (p * p) / ((p * p) + ((1.0 - p) * (1.0 - p)))
        if a == b:
            return deuce
        if a == b + 1:
            return p + ((1.0 - p) * deuce)
        if b == a + 1:
            return p * deuce

    return (
        p * _map_from_score(a + 1, b, rounded_round_p)
        + (1.0 - p) * _map_from_score(a, b + 1, rounded_round_p)
    )


def map_win_probability(rounds_a: int, rounds_b: int, round_probability_a: float) -> float:
    rounded = int(round(clamp_probability(round_probability_a) * 1_000_000))
    rounded = max(1, min(999_999, rounded))
    return _map_from_score(int(rounds_a), int(rounds_b), rounded)


def implied_round_probability(map_probability_a: float) -> float:
    target = clamp_probability(map_probability_a)
    low, high = 0.01, 0.99
    for _ in range(50):
        middle = (low + high) / 2.0
        value = map_win_probability(0, 0, middle)
        if value < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


@lru_cache(maxsize=1024)
def _fresh_series(maps_a: int, maps_b: int, best_of: int, rounded_map_p: int) -> float:
    needed = (best_of // 2) + 1
    if maps_a >= needed:
        return 1.0
    if maps_b >= needed:
        return 0.0
    p = rounded_map_p / 1_000_000.0
    return (
        p * _fresh_series(maps_a + 1, maps_b, best_of, rounded_map_p)
        + (1.0 - p) * _fresh_series(maps_a, maps_b + 1, best_of, rounded_map_p)
    )


def fresh_series_probability(maps_a: int, maps_b: int, best_of: int, map_probability_a: float) -> float:
    rounded = int(round(clamp_probability(map_probability_a) * 1_000_000))
    rounded = max(1, min(999_999, rounded))
    return _fresh_series(maps_a, maps_b, best_of, rounded)


def implied_map_probability(series_probability_a: float, best_of: int) -> float:
    target = clamp_probability(series_probability_a)
    low, high = 0.01, 0.99
    for _ in range(50):
        middle = (low + high) / 2.0
        value = fresh_series_probability(0, 0, best_of, middle)
        if value < target:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


@dataclass(frozen=True)
class ProbabilityBreakdown:
    model_version: str
    prior_series_probability_a: float
    implied_fresh_map_probability_a: float
    implied_fresh_round_probability_a: float
    adjusted_current_round_probability_a: float
    current_map_probability_a: float
    live_series_probability_a: float
    side_adjustment: float
    economy_adjustment: float
    map_adjustment: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def live_probability(match: Match, state: LiveState) -> ProbabilityBreakdown:
    match.validated()
    normalized = state.normalized()
    needed = (match.best_of // 2) + 1
    if normalized.maps_a >= needed:
        result = 1.0 - EPSILON
        fresh_map = implied_map_probability(match.prior_probability_a, match.best_of)
        fresh_round = implied_round_probability(fresh_map)
        return ProbabilityBreakdown(
            MODEL_VERSION,
            match.prior_probability_a,
            fresh_map,
            fresh_round,
            fresh_round,
            1.0,
            result,
            0.0,
            0.0,
            0.0,
        )
    if normalized.maps_b >= needed:
        result = EPSILON
        fresh_map = implied_map_probability(match.prior_probability_a, match.best_of)
        fresh_round = implied_round_probability(fresh_map)
        return ProbabilityBreakdown(
            MODEL_VERSION,
            match.prior_probability_a,
            fresh_map,
            fresh_round,
            fresh_round,
            0.0,
            result,
            0.0,
            0.0,
            0.0,
        )

    fresh_map = implied_map_probability(match.prior_probability_a, match.best_of)
    fresh_round = implied_round_probability(fresh_map)
    side_adjustment = 0.18 * normalized.side_advantage_a
    economy_adjustment = 0.35 * ((normalized.economy_a - normalized.economy_b) / 2.0)
    map_adjustment = 0.25 * normalized.map_bias_a
    adjusted_round = logistic(
        logit(fresh_round) + side_adjustment + economy_adjustment + map_adjustment
    )
    current_map = map_win_probability(
        normalized.rounds_a,
        normalized.rounds_b,
        adjusted_round,
    )
    after_a = fresh_series_probability(
        normalized.maps_a + 1,
        normalized.maps_b,
        match.best_of,
        fresh_map,
    )
    after_b = fresh_series_probability(
        normalized.maps_a,
        normalized.maps_b + 1,
        match.best_of,
        fresh_map,
    )
    series = (current_map * after_a) + ((1.0 - current_map) * after_b)
    return ProbabilityBreakdown(
        model_version=MODEL_VERSION,
        prior_series_probability_a=match.prior_probability_a,
        implied_fresh_map_probability_a=fresh_map,
        implied_fresh_round_probability_a=fresh_round,
        adjusted_current_round_probability_a=adjusted_round,
        current_map_probability_a=current_map,
        live_series_probability_a=clamp_probability(series),
        side_adjustment=side_adjustment,
        economy_adjustment=economy_adjustment,
        map_adjustment=map_adjustment,
    )

