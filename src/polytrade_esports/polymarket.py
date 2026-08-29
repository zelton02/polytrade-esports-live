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
            headers={"Accept": "application/json", "User-Agent": "polytrade-esports-live/0.5"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_books(self, token_ids: List[str]) -> List[Dict[str, Any]]:
        """Fetch both outcome books in one CLOB request.

        A paired snapshot avoids manufacturing a spread from two sequential
        network round trips during a fast market move.  The public endpoint is
        unauthenticated and returns the same full price/size arrays as /book.
        """
        tokens = [str(token).strip() for token in token_ids if str(token).strip()]
        if not tokens:
            raise ValueError("at least one Polymarket token id is required")
        payload = json.dumps([{"token_id": token} for token in tokens]).encode("utf-8")
        request = Request(
            "%s/books" % self.base_url,
            data=payload,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "polytrade-esports-live/0.5",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not isinstance(value, list):
            raise ValueError("Polymarket /books returned a non-list payload")
        return [dict(item) for item in value if isinstance(item, dict)]

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
        books = self.get_books([token_a, token_b])
        by_asset = {
            str(book.get("asset_id") or ""): book
            for book in books
            if str(book.get("asset_id") or "")
        }
        if token_a in by_asset and token_b in by_asset:
            book_a, book_b = by_asset[token_a], by_asset[token_b]
        elif len(books) == 2:
            # Older CLOB responses did not include asset_id but preserved the
            # request order.  Accept that legacy shape only when unambiguous.
            book_a, book_b = books
        else:
            raise ValueError("Polymarket /books did not return both outcome tokens")
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
