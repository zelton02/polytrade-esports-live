import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .timeutil import isoformat, parse_timestamp, utc_now
from .types import BookQuote

# Venue-vs-local clock lead treated as skew rather than a future observation.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=120)


class PolymarketBookClient:
    def __init__(self, base_url: str = "https://clob.polymarket.com", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    def get_book(self, token_id: str) -> Dict[str, Any]:
        query = urlencode({"token_id": token_id})
        request = Request(
            "%s/book?%s" % (self.base_url, query),
            headers={"Accept": "application/json", "User-Agent": "polytrade-esports-live/0.1"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _best(book: Dict[str, Any], side: str) -> float:
        levels: List[Dict[str, Any]] = list(book.get(side) or [])
        if not levels:
            raise ValueError("order book has no %s" % side)
        prices = [float(item["price"]) for item in levels]
        return max(prices) if side == "bids" else min(prices)

    @staticmethod
    def _timestamp(book: Dict[str, Any]) -> str:
        raw = str(book.get("timestamp") or "").strip()
        if raw.isdigit():
            value = int(raw)
            seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
            return isoformat(datetime.fromtimestamp(seconds, tz=timezone.utc))
        return isoformat(utc_now())

    def get_pair(self, match_id: str, token_a: str, token_b: str) -> BookQuote:
        if not token_a or not token_b:
            raise ValueError("both Polymarket token ids are required")
        book_a = self.get_book(token_a)
        book_b = self.get_book(token_b)
        observed_at = isoformat(utc_now())
        source_at = max(self._timestamp(book_a), self._timestamp(book_b))
        # The venue clock can lead ours by a second or two. That is skew, not a
        # future observation, so clamp it within a tight bound. A larger lead is
        # still rejected downstream by BookQuote.normalized().
        if source_at > observed_at:
            lead = parse_timestamp(source_at) - parse_timestamp(observed_at)
            if lead <= CLOCK_SKEW_TOLERANCE:
                source_at = observed_at
        return BookQuote(
            match_id=match_id,
            bid_a=self._best(book_a, "bids"),
            ask_a=self._best(book_a, "asks"),
            bid_b=self._best(book_b, "bids"),
            ask_b=self._best(book_b, "asks"),
            source_at=source_at,
            observed_at=observed_at,
            source="polymarket-clob-rest",
            raw={"A": book_a, "B": book_b},
        ).normalized()
