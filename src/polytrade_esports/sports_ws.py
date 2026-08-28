"""Polymarket Sports WebSocket adapter for live CS2 scores.

The Sports channel is a public, unfiltered stream.  Gamma exposes the same
numeric provider id as ``eventMetadata.pandascoreMatchId`` and Sports emits it
as ``gameId``, so matches are joined by id and team names are used only to
orient home/away scores into Polymarket outcome A/B order.

The connection runs in one background thread.  Collector cycles only read a
small, locked in-memory cache and never block on the socket.  A cached update
is deliberately unavailable while disconnected or after it becomes stale;
the caller can still record a maps-only forecast but must not open a new
in-play position from it.
"""

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from .timeutil import canonical_timestamp, isoformat, parse_timestamp, utc_now
from .types import LiveState

SPORTS_WS_URL = "wss://sports-api.polymarket.com/ws"
SOURCE = "polymarket-sports-ws"
MAPS_ONLY_SOURCE = "polymarket-sports-ws-maps"
TERMINAL_SOURCE = "polymarket-sports-ws-terminal"
CS2_LEAGUES = {"cs2", "csgo"}

_CS2_SCORE = re.compile(
    r"^\s*(\d+)-(\d+)\|(\d+)-(\d+)\|Bo([135])\s*$",
    re.IGNORECASE,
)
_GENERIC_TEAM_WORDS = {"club", "esport", "esports", "gaming", "team"}


def _team_key(value: Any) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    while tokens and tokens[0] in _GENERIC_TEAM_WORDS:
        tokens.pop(0)
    while tokens and tokens[-1] in _GENERIC_TEAM_WORDS:
        tokens.pop()
    return "".join(tokens)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _safe_source_at(payload: Dict[str, Any], received_at: str) -> str:
    raw = payload.get("last_update") or payload.get("lastUpdate")
    if not raw:
        return received_at
    try:
        source_at = canonical_timestamp(str(raw))
    except ValueError:
        return received_at
    # Provider clocks occasionally lead the collector. LiveState forbids a
    # source timestamp later than its observation, so clamp harmless skew.
    if parse_timestamp(source_at) > parse_timestamp(received_at):
        return received_at
    return source_at


@dataclass(frozen=True)
class SportsUpdate:
    game_id: str
    home_team: str
    away_team: str
    rounds_home: int
    rounds_away: int
    rounds_available: bool
    maps_home: int
    maps_away: int
    best_of: int
    period: str
    live: bool
    ended: bool
    source_at: str
    received_at: str
    raw: Dict[str, Any]


def parse_update(
    message: Any,
    received_at: Optional[str] = None,
) -> Optional[SportsUpdate]:
    """Parse one Sports message; return ``None`` for non-CS2/invalid input."""
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(message, str):
        if message.strip().lower() == "ping":
            return None
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return None
    elif isinstance(message, dict):
        payload = dict(message)
    else:
        return None
    if not isinstance(payload, dict):
        return None

    league = str(
        payload.get("leagueAbbreviation") or payload.get("league") or ""
    ).strip().lower()
    if league not in CS2_LEAGUES:
        return None
    game_id = str(payload.get("gameId") or "").strip()
    home_team = str(payload.get("homeTeam") or "").strip()
    away_team = str(payload.get("awayTeam") or "").strip()
    score = str(payload.get("score") or "")
    match = _CS2_SCORE.match(score)
    if not game_id or not home_team or not away_team or match is None:
        return None

    rounds_home, rounds_away, maps_home, maps_away, best_of = (
        int(value) for value in match.groups()
    )
    # Reject obviously corrupt snapshots before they can produce a near-0/1
    # forecast. Overtime can exceed 30 rounds, hence the conservative ceiling.
    if max(rounds_home, rounds_away) > 99:
        return None
    needed = best_of // 2 + 1
    if max(maps_home, maps_away) > needed or maps_home + maps_away > best_of:
        return None

    observed = received_at or isoformat(utc_now())
    try:
        observed = canonical_timestamp(observed)
    except ValueError:
        return None
    return SportsUpdate(
        game_id=game_id,
        home_team=home_team,
        away_team=away_team,
        rounds_home=rounds_home,
        rounds_away=rounds_away,
        # The live service uses exactly 000-000 as an unavailable placeholder.
        # A genuine score at the start of a map is emitted as 0-0.
        rounds_available=score.split("|", 1)[0].strip() != "000-000",
        maps_home=maps_home,
        maps_away=maps_away,
        best_of=best_of,
        period=str(payload.get("period") or ""),
        live=_as_bool(payload.get("live")),
        ended=_as_bool(payload.get("ended")),
        source_at=_safe_source_at(payload, observed),
        received_at=observed,
        raw=dict(payload),
    )


def _current_map(period: str, maps_home: int, maps_away: int) -> str:
    value = str(period or "").strip()
    match = re.match(r"^(\d+)\s*/\s*[135]$", value)
    if match:
        return "Map %d" % int(match.group(1))
    if value:
        return value
    return "Map %d" % (maps_home + maps_away + 1)


def build_state(
    update: SportsUpdate,
    match_id: str,
    team_a: str,
    team_b: str,
) -> LiveState:
    """Orient a Sports update into the market's A/B outcome order."""
    home = _team_key(update.home_team)
    away = _team_key(update.away_team)
    a = _team_key(team_a)
    b = _team_key(team_b)
    if not home or not away or not a or not b:
        raise ValueError("sports update has an empty team identity")
    if a == home and b == away:
        maps_a, maps_b = update.maps_home, update.maps_away
        rounds_a, rounds_b = update.rounds_home, update.rounds_away
    elif a == away and b == home:
        maps_a, maps_b = update.maps_away, update.maps_home
        rounds_a, rounds_b = update.rounds_away, update.rounds_home
    else:
        raise ValueError(
            "sports teams do not match market outcomes: %s / %s vs %s / %s"
            % (update.home_team, update.away_team, team_a, team_b)
        )
    needed = update.best_of // 2 + 1
    terminal = max(update.maps_home, update.maps_away) >= needed
    if terminal:
        source = TERMINAL_SOURCE
    elif update.rounds_available:
        source = SOURCE
    else:
        source = MAPS_ONLY_SOURCE
    return LiveState(
        match_id=match_id,
        source_at=update.source_at,
        observed_at=update.received_at,
        maps_a=maps_a,
        maps_b=maps_b,
        rounds_a=rounds_a,
        rounds_b=rounds_b,
        current_map=_current_map(update.period, update.maps_home, update.maps_away),
        source=source,
        raw=update.raw,
    ).normalized()


class SportsWebSocketAdapter:
    """Reconnectable Sports client with a thread-safe latest-update cache."""

    def __init__(
        self,
        url: str = SPORTS_WS_URL,
        max_age_seconds: float = 90.0,
        open_timeout: float = 10.0,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("sports max_age_seconds must be positive")
        self.url = str(url)
        self.max_age_seconds = float(max_age_seconds)
        self.open_timeout = float(open_timeout)
        self._updates: Dict[str, SportsUpdate] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._connection: Optional[Any] = None
        self._connected = False
        self._last_error = ""
        self._last_message_at = ""

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "url": self.url,
                "connected": self._connected,
                "updates": len(self._updates),
                "last_message_at": self._last_message_at,
                "last_error": self._last_error,
            }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="polymarket-sports-ws",
                daemon=True,
            )
            self._thread.start()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(max(0.0, float(timeout)))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._lock:
            connection = self._connection
            thread = self._thread
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        with self._lock:
            self._connected = False
            self._connection = None

    def ingest(
        self,
        message: Any,
        received_at: Optional[str] = None,
    ) -> Optional[SportsUpdate]:
        update = parse_update(message, received_at=received_at)
        if update is None:
            return None
        with self._lock:
            self._updates[update.game_id] = update
            self._last_message_at = update.received_at
        self._ready.set()
        return update

    def state_for(
        self,
        provider_match_id: str,
        match_id: str,
        team_a: str,
        team_b: str,
        now: Optional[datetime] = None,
    ) -> Optional[LiveState]:
        """Return fresh live state, or ``None`` when the feed isn't trustworthy."""
        with self._lock:
            connected = self._connected
            update = self._updates.get(str(provider_match_id))
        if not connected or update is None or not update.live or update.ended:
            return None
        reference = now or utc_now()
        age = (reference - parse_timestamp(update.received_at)).total_seconds()
        if age < -5.0 or age > self.max_age_seconds:
            return None
        return build_state(update, match_id, team_a, team_b)

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                # Lazy import keeps parser/unit tests usable before optional
                # runtime dependencies are installed by the container image.
                from websockets.sync.client import connect

                with connect(
                    self.url,
                    open_timeout=self.open_timeout,
                    close_timeout=3,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=1_048_576,
                    proxy=None,
                ) as websocket:
                    with self._lock:
                        self._connection = websocket
                        self._connected = True
                        self._last_error = ""
                    delay = 1.0
                    while not self._stop.is_set():
                        try:
                            message = websocket.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        if isinstance(message, bytes):
                            heartbeat = message.strip().lower() == b"ping"
                        else:
                            heartbeat = str(message).strip().lower() == "ping"
                        if heartbeat:
                            websocket.send("pong")
                            continue
                        self.ingest(message)
            except Exception as error:
                with self._lock:
                    self._last_error = "%s: %s" % (type(error).__name__, error)
            finally:
                with self._lock:
                    self._connected = False
                    self._connection = None
            if self._stop.wait(delay):
                return
            delay = min(30.0, delay * 2.0)
