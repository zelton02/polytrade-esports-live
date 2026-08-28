"""Polymarket Gamma discovery for CS2 match markets.

Gamma exposes one *event* per esports match. Each event carries several
markets; the series winner is the one tagged ``sportsMarketType == "moneyline"``.
Only that market's two CLOB token ids are tradable series outcomes, so the
discovery layer deliberately ignores map-winner, totals and handicap markets
except as a fallback signal for the map score.
"""

import json
import re
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .timeutil import canonical_timestamp, parse_timestamp, utc_now
from .types import Match

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CS2_SLUG_PREFIX = "cs2-"
SERIES_MARKET_TYPE = "moneyline"
MAP_MARKET_TYPE = "child_moneyline"
PAGE_SIZE = 100


def _loads(value: Any) -> Any:
    """Gamma returns several list fields as JSON-encoded strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


class GammaClient:
    def __init__(self, base_url: str = GAMMA_BASE_URL, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        request = Request(
            "%s%s?%s" % (self.base_url, path, urlencode(params)),
            headers={
                "Accept": "application/json",
                "User-Agent": "polytrade-esports-live/0.2",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def iter_esports_events(self, max_pages: int = 6) -> Iterator[Dict[str, Any]]:
        for page in range(max_pages):
            batch = self._get(
                "/events",
                {
                    "closed": "false",
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "tag_slug": "esports",
                    "order": "startDate",
                    "ascending": "false",
                },
            )
            if not isinstance(batch, list) or not batch:
                return
            for event in batch:
                yield event
            if len(batch) < PAGE_SIZE:
                return

    def get_event(self, slug: str) -> Optional[Dict[str, Any]]:
        batch = self._get("/events", {"slug": slug})
        if isinstance(batch, list) and batch:
            return batch[0]
        return None

    def cs2_events(self, max_pages: int = 6) -> List[Dict[str, Any]]:
        return [
            event
            for event in self.iter_esports_events(max_pages=max_pages)
            if str(event.get("slug") or "").startswith(CS2_SLUG_PREFIX)
        ]


def series_market(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for market in event.get("markets") or []:
        if market.get("sportsMarketType") == SERIES_MARKET_TYPE:
            return market
    return None


def map_markets(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    markets = [
        market
        for market in event.get("markets") or []
        if market.get("sportsMarketType") == MAP_MARKET_TYPE
    ]
    return sorted(markets, key=lambda item: str(item.get("groupItemTitle") or ""))


def best_of(event: Dict[str, Any]) -> int:
    """Read the series length from ``score`` ("0-0|0-0|Bo3") or the title."""
    for candidate in (str(event.get("score") or ""), str(event.get("title") or "")):
        upper = candidate.upper()
        for value in (5, 3, 1):
            if "BO%d" % value in upper:
                return value
    period = str(event.get("period") or "")
    if "/" in period:
        try:
            total = int(period.split("/", 1)[1])
        except ValueError:
            total = 0
        if total in (1, 3, 5):
            return total
    return 3


def parse_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one Gamma event into a discovery record, or ``None``.

    Returns ``None`` when the event cannot be traded as a two-outcome series
    (missing moneyline market, missing token ids, or non-binary outcomes).
    """
    market = series_market(event)
    if market is None:
        return None
    outcomes = _loads(market.get("outcomes"))
    tokens = _loads(market.get("clobTokenIds"))
    if not isinstance(outcomes, list) or not isinstance(tokens, list):
        return None
    if len(outcomes) != 2 or len(tokens) != 2:
        return None
    team_a, team_b = str(outcomes[0]).strip(), str(outcomes[1]).strip()
    if not team_a or not team_b or team_a == team_b:
        return None
    prices = _loads(market.get("outcomePrices")) or []
    try:
        market_price_a = float(prices[0])
    except (IndexError, TypeError, ValueError):
        market_price_a = None
    metadata = event.get("eventMetadata") or {}
    scheduled_raw = event.get("startTime") or event.get("gameStartTime")
    scheduled_at = None
    if scheduled_raw:
        try:
            scheduled_at = canonical_timestamp(str(scheduled_raw).replace(" ", "T"))
        except ValueError:
            scheduled_at = None
    return {
        "match_id": str(event.get("slug") or "").strip(),
        "event_id": str(event.get("id") or ""),
        "team_a": team_a,
        "team_b": team_b,
        "best_of": best_of(event),
        "token_a": str(tokens[0]),
        "token_b": str(tokens[1]),
        "condition_id": str(market.get("conditionId") or ""),
        "market_price_a": market_price_a,
        "title": str(event.get("title") or ""),
        "league": str(metadata.get("league") or ""),
        "serie": str(metadata.get("serie") or ""),
        "tournament": str(metadata.get("tournament") or ""),
        "context": str(metadata.get("context_description") or ""),
        "pandascore_match_id": metadata.get("pandascoreMatchId"),
        "scheduled_at": scheduled_at,
        "live": bool(event.get("live")),
        "ended": bool(event.get("ended")),
        "period": str(event.get("period") or ""),
        "score": str(event.get("score") or ""),
        "liquidity": float(event.get("liquidity") or 0.0),
        "resolution_source": str(event.get("resolutionSource") or ""),
        "map_score": map_score_from_markets(event, team_a, team_b),
    }


def map_score_from_markets(
    event: Dict[str, Any], team_a: str, team_b: str
) -> Dict[str, int]:
    """Derive maps won from resolved per-map markets.

    A closed map market whose winning outcome price is 1 identifies the map
    winner. This is the fallback used when no live provider is configured; it
    updates only at map boundaries, never mid-map.
    """
    maps_a = 0
    maps_b = 0
    for market in map_markets(event):
        if not market.get("closed"):
            continue
        outcomes = _loads(market.get("outcomes"))
        prices = _loads(market.get("outcomePrices"))
        if not isinstance(outcomes, list) or not isinstance(prices, list):
            continue
        if len(outcomes) != len(prices):
            continue
        for outcome, price in zip(outcomes, prices):
            try:
                value = float(price)
            except (TypeError, ValueError):
                continue
            if value < 0.99:
                continue
            if str(outcome).strip() == team_a:
                maps_a += 1
            elif str(outcome).strip() == team_b:
                maps_b += 1
    return {"maps_a": maps_a, "maps_b": maps_b}


def final_map_score(
    event: Dict[str, Any], team_a: str, team_b: str
) -> Optional[Dict[str, int]]:
    """Return a validated terminal series score for an ended fixture.

    Resolved per-map markets are the preferred source. Gamma's compact score
    field is a fallback for the short interval where the series result has
    landed but one of the child markets has not been exposed as resolved yet.
    A score is accepted only when one side has reached the number of maps
    required to win the advertised best-of series.
    """
    if not event.get("ended"):
        return None

    series_length = best_of(event)
    needed = series_length // 2 + 1

    maps = map_score_from_markets(event, team_a, team_b)
    maps_a, maps_b = int(maps["maps_a"]), int(maps["maps_b"])
    if max(maps_a, maps_b) == needed and maps_a + maps_b <= series_length:
        return maps

    # Typical Gamma value: "000-000|2-0|Bo3". Select only a pair that can be
    # the completed series score, which rejects the leading round/placeholder
    # segment and avoids treating an in-map score as a final result.
    for left, right in re.findall(r"(?<!\d)(\d+)-(\d+)(?!\d)", str(event.get("score") or "")):
        score_a, score_b = int(left), int(right)
        if max(score_a, score_b) == needed and score_a + score_b <= series_length:
            return {"maps_a": score_a, "maps_b": score_b}
    return None


def to_match(record: Dict[str, Any], prior_probability_a: float) -> Match:
    return Match(
        match_id=record["match_id"],
        team_a=record["team_a"],
        team_b=record["team_b"],
        best_of=record["best_of"],
        prior_probability_a=prior_probability_a,
        token_a=record["token_a"],
        token_b=record["token_b"],
        source="polymarket-gamma",
        external_id=record["event_id"],
        scheduled_at=record["scheduled_at"],
    ).validated()

# A closed market prices the winning outcome at 1 and the loser at 0. Anything
# in between means the market has not actually decided yet.
DECIDED_PRICE = 0.99


def resolution_from_event(
    event: Dict[str, Any], team_a: str, team_b: str
) -> Optional[Dict[str, Any]]:
    """Read the settled series result, or ``None`` if it is not decided yet.

    Returns ``{"winner": "A"|"B"}`` for a decided series, or
    ``{"winner": None, "void": True}`` when the market closed without a winner
    (cancelled or forfeited fixtures do this).

    Outcomes are matched by team name rather than by list position. Position
    would silently invert the result if Polymarket ever reordered the pair, and
    an inverted winner corrupts every score computed from it.
    """
    if not event.get("ended"):
        return None
    market = series_market(event)
    if market is None or not market.get("closed"):
        return None
    outcomes = _loads(market.get("outcomes"))
    prices = _loads(market.get("outcomePrices"))
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) != len(prices) or not outcomes:
        return None

    winner = None
    for outcome, price in zip(outcomes, prices):
        try:
            value = float(price)
        except (TypeError, ValueError):
            return None
        if value < DECIDED_PRICE:
            continue
        name = str(outcome).strip()
        if name == str(team_a).strip():
            winner = "A"
        elif name == str(team_b).strip():
            winner = "B"
        else:
            # The market resolved to a team this match was never about.
            return None
    if winner is None:
        return {"winner": None, "void": True}
    return {"winner": winner, "void": False}


def is_stale(record: Dict[str, Any], max_age_hours: float = 12.0) -> bool:
    """True when a discovered match is too old to be worth tracking.

    Polymarket leaves esports events with ``closed: false`` long after the
    fixture is over while UMA settlement is still pending, so a plain
    ``closed=false`` sweep drags in months of finished matches.
    """
    if record.get("ended"):
        return True
    scheduled = record.get("scheduled_at")
    if not scheduled:
        return False
    try:
        starts_at = parse_timestamp(scheduled)
    except ValueError:
        return False
    age_hours = (utc_now() - starts_at).total_seconds() / 3600.0
    return age_hours > max_age_hours
