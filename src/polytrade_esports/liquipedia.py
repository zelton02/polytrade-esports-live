"""Liquipedia facts for the pre-match prior.

The point of this module is that the model should not be asked to *recall* or
*search for* team information. It gets told, from a source we fetched and can
cite. An LLM given a market's own summary will paraphrase it back with a
confident number attached; an LLM given a roster with join dates and a list of
dated results has something to reason from that the market did not hand it.

Only the free MediaWiki API is used. ``action=parse`` renders the results table
that is otherwise stored in Liquipedia's database, so no paid tier is needed.

Rate limits are enforced here rather than left to the caller, because the
published terms are strict and violating them earns an automated IP ban:
one request per 2 seconds overall, one ``action=parse`` per 30 seconds, and no
more than 60 requests per hour. The User-Agent must identify the project and
carry contact details; generic agents are explicitly listed as ban-worthy.
"""

import gzip
import html as html_module
import json
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://liquipedia.net/counterstrike/api.php"
USER_AGENT = (
    "polytrade-esports-live/0.2 "
    "(CS2 calibration research; https://esports.zhng.tech; zhi.heng426@gmail.com)"
)
MIN_INTERVAL_SECONDS = 2.0
PARSE_INTERVAL_SECONDS = 30.0
HOURLY_BUDGET = 60
# "0 : 2" for a series, "13 - 11" for a single map.
SCORE_PATTERN = re.compile(r"^\d{1,2}\s*[:\-]\s*\d{1,2}$")


class LiquipediaError(RuntimeError):
    pass


class _Throttle:
    """Process-wide gate for the published limits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_any = 0.0
        self._last_parse = 0.0
        self._hour: List[float] = []

    def wait(self, is_parse: bool) -> None:
        with self._lock:
            now = time.time()
            self._hour = [t for t in self._hour if now - t < 3600.0]
            if len(self._hour) >= HOURLY_BUDGET:
                raise LiquipediaError(
                    "hourly request budget of %d reached; refusing to exceed the "
                    "published rate limit" % HOURLY_BUDGET
                )
            delay = max(
                MIN_INTERVAL_SECONDS - (now - self._last_any),
                (PARSE_INTERVAL_SECONDS - (now - self._last_parse)) if is_parse else 0.0,
                0.0,
            )
            if delay > 0:
                time.sleep(delay)
                now = time.time()
            self._last_any = now
            if is_parse:
                self._last_parse = now
            self._hour.append(now)


_THROTTLE = _Throttle()


class _MatchTableParser(HTMLParser):
    """Pull result rows out of the rendered Matches page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Dict[str, Any]] = []
        self._cell: Optional[List[str]] = None
        self._row: Optional[List[str]] = None
        self._timestamp: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        values = dict(attrs)
        if tag == "tr" and "table2__row--body" in (values.get("class") or ""):
            self._row = []
            self._timestamp = None
        elif tag == "td" and self._row is not None:
            self._cell = []
        elif tag == "span" and self._row is not None and values.get("data-timestamp"):
            self._timestamp = values["data-timestamp"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            cells = [cell for cell in self._row if cell]
            if len(cells) >= 6:
                self.rows.append({"timestamp": self._timestamp, "cells": cells})
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _normalize_title(value: str) -> str:
    """Lowercase, drop punctuation and the noise words teams get listed under."""
    text = re.sub(r"\(.*?\)", " ", str(value or "").lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\b(esports|esport|gaming|team|club|the)\b", " ", text)
    return " ".join(text.split())


# Qualifiers that make a page a different squad from the one being asked about.
_DISTINCT_SQUAD = re.compile(
    r"\b(female|fe|academy|junior|youth|second|reserve|women|w)\b", re.I
)


def _titles_match(title: str, name: str) -> bool:
    """True when a page title plausibly names the same team.

    Deliberately strict. A near-miss here is a page of facts about somebody
    else presented to the model as verified.
    """
    if "/" in title:
        # Subpages are tournaments, seasons and brackets, never a team.
        return False
    left, right = _normalize_title(title), _normalize_title(name)
    if not left or not right:
        return False
    if _DISTINCT_SQUAD.search(title) and not _DISTINCT_SQUAD.search(name):
        return False
    return left == right or left.startswith(right + " ") or right.startswith(left + " ")


class LiquipediaClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 40.0) -> None:
        self.base_url = base_url
        self.timeout = float(timeout)

    def _get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params.setdefault("format", "json")
        _THROTTLE.wait(is_parse=params.get("action") == "parse")
        request = Request(
            "%s?%s" % (self.base_url, urlencode(params)),
            headers={
                "User-Agent": USER_AGENT,
                # The API rejects uncompressed requests with 406.
                "Accept-Encoding": "gzip",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as error:
            raise LiquipediaError("Liquipedia request failed: %s" % error) from error

    def candidate_titles(self, name: str) -> List[str]:
        """Existing page titles for a team, best guess first.

        A list rather than one answer because an existing page is not
        necessarily the right one: "Spirit" exists as a disambiguation page
        while the roster lives on "Team Spirit". The caller picks by checking
        which candidate actually has a squad, which is a cheap query, before
        spending the expensive rendered-results request.

        Only the "ex-" prefix Polymarket adds for a disbanded lineup is
        stripped; "Team" is part of plenty of real names.
        """
        raw = str(name or "").strip()
        if not raw:
            return []
        stripped = re.sub(r"^ex-", "", raw, flags=re.I).strip()
        candidates = []
        for value in (raw, stripped, "Team %s" % stripped):
            if value and value not in candidates:
                candidates.append(value)

        payload = self._get(
            {"action": "query", "titles": "|".join(candidates), "redirects": 1}
        )
        query = payload.get("query", {})
        existing = {
            page["title"]
            for page in query.get("pages", {}).values()
            if "missing" not in page
        }
        ordered: List[str] = []
        if existing:
            normalized = {n["from"]: n["to"] for n in query.get("normalized", [])}
            redirects = {r["from"]: r["to"] for r in query.get("redirects", [])}
            for value in candidates:
                title = redirects.get(normalized.get(value, value),
                                      normalized.get(value, value))
                if title in existing and title not in ordered and _titles_match(title, value):
                    ordered.append(title)

        payload = self._get(
            {"action": "query", "list": "search", "srsearch": stripped, "srlimit": 5}
        )
        hits = [item["title"] for item in payload.get("query", {}).get("search", [])]
        lowered = stripped.lower()
        for title in sorted(hits, key=lambda t: (t.lower() != lowered, len(t))):
            # Full-text search happily returns a tournament bracket or a
            # different roster that merely mentions the name. Observed:
            # "Turma do Pagode" returned an ESEA season page, and "Imperial"
            # returned "Imperial Female" -- a real team, real roster, wrong
            # match. Wrong facts are worse than none, because they read as
            # verified, so only a title that plausibly names this team is
            # accepted and everything else is discarded.
            if title not in ordered and _titles_match(title, stripped):
                ordered.append(title)
        return ordered[:4]

    def roster(self, title: str) -> List[Dict[str, str]]:
        """Active players with join dates, from the page wikitext."""
        payload = self._get(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": title,
            }
        )
        pages = payload.get("query", {}).get("pages", {})
        for page in pages.values():
            revisions = page.get("revisions") or []
            if not revisions:
                continue
            content = revisions[0].get("slots", {}).get("main", {}).get("*", "")
            block = content
            active = re.search(r"\{\{Squad\|status=active(.*?)\n\}\}", content, re.S)
            if active:
                block = active.group(1)
            players = []
            for match in re.finditer(
                r"\{\{Person\|flag=\w+\s*\|id=([^|]+)\|name=([^|]*)\|joindate=([\d-]+)",
                block,
            ):
                players.append(
                    {
                        "id": match.group(1).strip(),
                        "name": match.group(2).strip(),
                        "joined": match.group(3),
                    }
                )
            return players[:8]
        return []

    def recent_matches(
        self, title: str, limit: int = 12, before_timestamp: Optional[float] = None
    ) -> Dict[str, Any]:
        """Recent results plus the aggregate win rates shown on the page.

        ``before_timestamp`` drops anything at or after that instant. This is
        not a nicety: Liquipedia lists the fixture being forecast as soon as it
        finishes, so an unfiltered fetch hands the model the answer to the
        question it is being asked. Observed in testing -- the result of the
        very match under forecast appeared in its own evidence block. The model
        happened to notice and discount it, which is exactly the kind of thing
        that must not be left to the model to police.
        """
        payload = self._get(
            {"action": "parse", "page": "%s/Matches" % title, "prop": "text"}
        )
        html = payload.get("parse", {}).get("text", {}).get("*", "")
        if not html:
            return {"matches": [], "record": None}

        parser = _MatchTableParser()
        parser.feed(html)
        matches = []
        dropped = 0
        for row in parser.rows:
            if len(matches) >= limit:
                break
            if before_timestamp is not None and row["timestamp"]:
                try:
                    if float(row["timestamp"]) >= float(before_timestamp):
                        dropped += 1
                        continue
                except (TypeError, ValueError):
                    # An unparseable stamp cannot be shown to be in the past,
                    # so it is dropped rather than risked.
                    dropped += 1
                    continue
            cells = row["cells"]
            # Column positions drift between team pages (an extra icon column
            # here, a missing VOD column there), so the score is located by
            # what it looks like rather than by index; the opponent is then
            # the cell after it.
            score_at = None
            for index, cell in enumerate(cells):
                if SCORE_PATTERN.match(cell):
                    score_at = index
                    break
            matches.append(
                {
                    "played_at": cells[0] if cells else "",
                    "timestamp": row["timestamp"],
                    "tier": cells[1] if len(cells) > 1 else "",
                    "tournament": cells[3] if len(cells) > 3 else "",
                    "score": cells[score_at] if score_at is not None else "",
                    "opponent": (
                        cells[score_at + 1]
                        if score_at is not None and score_at + 1 < len(cells)
                        else ""
                    ),
                }
            )

        record = None
        text = html_module.unescape(html)
        pattern = (
            r"(\d+)W[^0-9]{0,12}(\d+)L \(([\d.]+)%\) in matches and "
            r"(\d+)W[^0-9]{0,12}(\d+)L \(([\d.]+)%\) in games and "
            r"(\d+)W[^0-9]{0,12}(\d+)L \(([\d.]+)%\) in rounds"
        )
        found = re.search(pattern, text)
        if found:
            record = {
                "match_win_pct": float(found.group(3)),
                "map_win_pct": float(found.group(6)),
                "round_win_pct": float(found.group(9)),
                "matches_won": int(found.group(1)),
                "matches_lost": int(found.group(2)),
            }
        return {"matches": matches, "record": record, "dropped_after_cutoff": dropped}

    def team_facts(
        self,
        name: str,
        match_limit: int = 10,
        before_timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Everything worth putting in a prompt about one team.

        Failures are returned rather than raised: a prior with one team's facts
        is still better grounded than one with none, and a wiki page that does
        not exist for a tier-5 roster is the normal case, not an error.
        """
        facts: Dict[str, Any] = {
            "queried_name": name,
            "page": None,
            "roster": [],
            "recent": [],
            "record": None,
            "dropped_after_cutoff": 0,
            "error": None,
        }
        try:
            candidates = self.candidate_titles(name)
            if not candidates:
                facts["error"] = "no Liquipedia page found"
                return facts

            # An empty squad means the page is a disambiguation or a stub, not
            # the team. Checking that costs one cheap query; committing to it
            # would waste the 30-second results request.
            chosen = None
            roster: List[Dict[str, str]] = []
            for title in candidates:
                found = self.roster(title)
                if found:
                    chosen, roster = title, found
                    break
            if chosen is None:
                facts["page"] = candidates[0]
                facts["error"] = "no active roster on any candidate page"
                return facts

            facts["page"] = chosen
            facts["roster"] = roster
            results = self.recent_matches(
                chosen, limit=match_limit, before_timestamp=before_timestamp
            )
            facts["recent"] = results["matches"]
            facts["record"] = results["record"]
            facts["dropped_after_cutoff"] = results.get("dropped_after_cutoff", 0)
        except LiquipediaError as error:
            facts["error"] = str(error)
        return facts
