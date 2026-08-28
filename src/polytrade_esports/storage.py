import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .timeutil import canonical_timestamp, isoformat, parse_timestamp, utc_now
from .types import BookQuote, LiveState, Match


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    game TEXT NOT NULL DEFAULT 'cs2',
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    best_of INTEGER NOT NULL CHECK (best_of IN (1, 3, 5)),
    prior_probability_a REAL NOT NULL CHECK (prior_probability_a > 0 AND prior_probability_a < 1),
    token_a TEXT NOT NULL DEFAULT '',
    token_b TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'void')),
    winner TEXT CHECK (winner IN ('A', 'B') OR winner IS NULL),
    resolved_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    state_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    source TEXT NOT NULL,
    source_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    maps_a INTEGER NOT NULL,
    maps_b INTEGER NOT NULL,
    rounds_a INTEGER NOT NULL,
    rounds_b INTEGER NOT NULL,
    current_map TEXT NOT NULL,
    side_advantage_a REAL NOT NULL,
    economy_a REAL NOT NULL,
    economy_b REAL NOT NULL,
    map_bias_a REAL NOT NULL,
    raw_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE (match_id, source, source_at, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_state_match_time
ON state_snapshots(match_id, source_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    book_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    source TEXT NOT NULL,
    source_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    bid_a REAL NOT NULL,
    ask_a REAL NOT NULL,
    bid_b REAL NOT NULL,
    ask_b REAL NOT NULL,
    raw_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    UNIQUE (match_id, source, source_at, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_book_match_time
ON market_snapshots(match_id, source_at);

CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    state_id INTEGER NOT NULL REFERENCES state_snapshots(state_id),
    book_id INTEGER NOT NULL REFERENCES market_snapshots(book_id),
    forecast_at TEXT NOT NULL,
    model_version TEXT NOT NULL,
    probability_a REAL NOT NULL CHECK (probability_a > 0 AND probability_a < 1),
    market_midpoint_a REAL NOT NULL,
    edge_a REAL NOT NULL,
    edge_b REAL NOT NULL,
    best_side TEXT CHECK (best_side IN ('A', 'B') OR best_side IS NULL),
    breakdown_json TEXT NOT NULL,
    UNIQUE (state_id, book_id, model_version)
);

CREATE INDEX IF NOT EXISTS idx_forecast_match_time
ON forecasts(match_id, forecast_at);

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    initial_cash REAL NOT NULL,
    cash REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id INTEGER NOT NULL REFERENCES paper_accounts(account_id),
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('A', 'B')),
    shares REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, match_id, outcome)
);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES paper_accounts(account_id),
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    forecast_id INTEGER NOT NULL REFERENCES forecasts(forecast_id),
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'SETTLE')),
    outcome TEXT NOT NULL CHECK (outcome IN ('A', 'B')),
    shares REAL NOT NULL,
    price REAL NOT NULL,
    cash_delta REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    traded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trade_account_time
ON paper_trades(account_id, traded_at);
"""

# Schema additions for live discovery and LLM priors. Kept separate from SCHEMA
# so an existing V0 database upgrades in place rather than being rebuilt.
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS llm_priors (
    prior_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    created_at TEXT NOT NULL,
    evidence_cutoff_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    probability_a REAL NOT NULL CHECK (probability_a > 0 AND probability_a < 1),
    raw_probability_a REAL NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('low', 'medium', 'high')),
    reasoning_summary TEXT NOT NULL,
    key_factors_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    prompt_sha256 TEXT NOT NULL DEFAULT '',
    applied INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_prior_match_time
ON llm_priors(match_id, created_at);

CREATE TABLE IF NOT EXISTS collector_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    discovered INTEGER NOT NULL DEFAULT 0,
    ticked INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_collector_started
ON collector_runs(started_at);
"""

# The market's own probability at the instant the prior was made. Scoring the
# AI against a price sampled at some other moment is not a fair comparison.
PRIOR_COLUMNS = (
    ("market_probability_a", "REAL"),
    # Which adapter produced the prior. `provider` records the LLM vendor and
    # reads "deepseek" for both a Hermes run and a direct API run, so it cannot
    # separate the cohorts -- and comparing a web-researched cohort against a
    # blind one is the whole point of keeping both.
    ("backend", "TEXT NOT NULL DEFAULT ''"),
)

COLLECTOR_COLUMNS = (
    # Expected provider limitations are not failures, but hiding them makes a
    # maps-only feed look like a round-level feed. Keep them with every run so
    # the dashboard can state the effective capability honestly.
    ("notices_json", "TEXT NOT NULL DEFAULT '[]'"),
)

# Esports start times slip routinely; a prior written just after the scheduled
# time is still a pre-match prior in practice.
GRACE_MINUTES = 20

# ALTER TABLE has no IF NOT EXISTS in SQLite; applied only when absent.
MATCH_COLUMNS = (
    ("league", "TEXT NOT NULL DEFAULT ''"),
    ("serie", "TEXT NOT NULL DEFAULT ''"),
    ("tournament", "TEXT NOT NULL DEFAULT ''"),
    ("title", "TEXT NOT NULL DEFAULT ''"),
    ("context", "TEXT NOT NULL DEFAULT ''"),
    ("provider_match_id", "TEXT NOT NULL DEFAULT ''"),
    ("condition_id", "TEXT NOT NULL DEFAULT ''"),
    ("live", "INTEGER NOT NULL DEFAULT 0"),
    # Fixture lifecycle and market settlement are deliberately separate. A CS2
    # fixture can be over for hours before Polymarket resolves the moneyline.
    ("ended", "INTEGER NOT NULL DEFAULT 0"),
    ("finished_observed_at", "TEXT"),
    ("liquidity", "REAL NOT NULL DEFAULT 0"),
    ("prior_source", "TEXT NOT NULL DEFAULT 'seed'"),
    ("updated_at", "TEXT"),
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: str):
        self.path = str(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.executescript(SCHEMA_V2)
            self._migrate_columns(connection, "matches", MATCH_COLUMNS)
            self._migrate_columns(connection, "llm_priors", PRIOR_COLUMNS)
            self._migrate_columns(connection, "collector_runs", COLLECTOR_COLUMNS)
            connection.execute(
                """
                UPDATE matches
                SET live=0, ended=1,
                    finished_observed_at=COALESCE(finished_observed_at, resolved_at)
                WHERE status IN ('resolved', 'void')
                """
            )
            self._label_legacy_backends(connection)
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                ("schema_version", "3"),
            )

    @staticmethod
    def _label_legacy_backends(connection: sqlite3.Connection) -> None:
        """Backfill the backend label on priors written before it existed.

        The rule is sound rather than a guess: a no-web backend is instructed to
        leave supporting_evidence empty and never to invent a URL, so a stored
        prior carrying evidence can only have come from a web-capable run.
        """
        connection.execute(
            """
            UPDATE llm_priors SET backend='hermes'
            WHERE backend='' AND evidence_json <> '[]'
            """
        )
        connection.execute(
            """
            UPDATE llm_priors SET backend='deepseek'
            WHERE backend='' AND evidence_json = '[]'
            """
        )

    @staticmethod
    def _migrate_columns(
        connection: sqlite3.Connection, table: str, columns: Any
    ) -> None:
        present = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(%s)" % table).fetchall()
        }
        for name, definition in columns:
            if name not in present:
                connection.execute(
                    "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition)
                )

    def add_match(self, match: Match) -> None:
        match.validated()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM matches WHERE match_id=?", (match.match_id,)
            ).fetchone()
            values = (
                match.match_id,
                match.team_a,
                match.team_b,
                match.best_of,
                match.prior_probability_a,
                match.token_a,
                match.token_b,
                match.source,
                match.external_id,
                match.scheduled_at,
                isoformat(utc_now()),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO matches(
                        match_id, team_a, team_b, best_of, prior_probability_a,
                        token_a, token_b, source, external_id, scheduled_at, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                return
            comparable = (
                existing["team_a"],
                existing["team_b"],
                existing["best_of"],
                existing["prior_probability_a"],
                existing["token_a"],
                existing["token_b"],
            )
            requested = (
                match.team_a,
                match.team_b,
                match.best_of,
                match.prior_probability_a,
                match.token_a,
                match.token_b,
            )
            if comparable != requested:
                raise ValueError("existing match differs; use a new match_id to preserve the frozen prior")

    def get_match(self, match_id: str) -> Match:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM matches WHERE match_id=?", (match_id,)
            ).fetchone()
        if row is None:
            raise KeyError("unknown match_id: %s" % match_id)
        return Match(
            match_id=row["match_id"],
            team_a=row["team_a"],
            team_b=row["team_b"],
            best_of=row["best_of"],
            prior_probability_a=row["prior_probability_a"],
            token_a=row["token_a"],
            token_b=row["token_b"],
            source=row["source"],
            external_id=row["external_id"],
            scheduled_at=row["scheduled_at"],
        )

    def latest_state(self, match_id: str) -> LiveState:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM state_snapshots
                WHERE match_id=? ORDER BY source_at DESC, state_id DESC LIMIT 1
                """,
                (match_id,),
            ).fetchone()
        if row is None:
            raise KeyError("no state snapshot for match_id: %s" % match_id)
        return LiveState(
            match_id=row["match_id"],
            source_at=row["source_at"],
            observed_at=row["observed_at"],
            maps_a=row["maps_a"],
            maps_b=row["maps_b"],
            rounds_a=row["rounds_a"],
            rounds_b=row["rounds_b"],
            current_map=row["current_map"],
            side_advantage_a=row["side_advantage_a"],
            economy_a=row["economy_a"],
            economy_b=row["economy_b"],
            map_bias_a=row["map_bias_a"],
            source=row["source"],
            raw=json.loads(row["raw_json"]),
        )

    def latest_forecast_id(self, match_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT forecast_id FROM forecasts
                WHERE match_id=? ORDER BY forecast_at DESC, forecast_id DESC LIMIT 1
                """,
                (match_id,),
            ).fetchone()
        if row is None:
            raise KeyError("no forecast for match_id: %s" % match_id)
        return int(row["forecast_id"])

    def record_state(self, state: LiveState) -> int:
        normalized = state.normalized()
        payload = normalized.to_dict()
        digest = _sha(payload)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO state_snapshots(
                    match_id, source, source_at, observed_at, ingested_at,
                    maps_a, maps_b, rounds_a, rounds_b, current_map,
                    side_advantage_a, economy_a, economy_b, map_bias_a,
                    raw_json, payload_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.match_id,
                    normalized.source,
                    normalized.source_at,
                    normalized.observed_at,
                    isoformat(utc_now()),
                    normalized.maps_a,
                    normalized.maps_b,
                    normalized.rounds_a,
                    normalized.rounds_b,
                    normalized.current_map,
                    normalized.side_advantage_a,
                    normalized.economy_a,
                    normalized.economy_b,
                    normalized.map_bias_a,
                    _json(normalized.raw or {}),
                    digest,
                ),
            )
            row = connection.execute(
                """
                SELECT state_id FROM state_snapshots
                WHERE match_id=? AND source=? AND source_at=? AND payload_sha256=?
                """,
                (normalized.match_id, normalized.source, normalized.source_at, digest),
            ).fetchone()
        return int(row["state_id"])

    def record_book(self, quote: BookQuote) -> int:
        normalized = quote.normalized()
        payload = normalized.to_dict()
        digest = _sha(payload)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO market_snapshots(
                    match_id, source, source_at, observed_at, ingested_at,
                    bid_a, ask_a, bid_b, ask_b, raw_json, payload_sha256
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.match_id,
                    normalized.source,
                    normalized.source_at,
                    normalized.observed_at,
                    isoformat(utc_now()),
                    normalized.bid_a,
                    normalized.ask_a,
                    normalized.bid_b,
                    normalized.ask_b,
                    _json(normalized.raw or {}),
                    digest,
                ),
            )
            row = connection.execute(
                """
                SELECT book_id FROM market_snapshots
                WHERE match_id=? AND source=? AND source_at=? AND payload_sha256=?
                """,
                (normalized.match_id, normalized.source, normalized.source_at, digest),
            ).fetchone()
        return int(row["book_id"])

    def record_forecast(
        self,
        match_id: str,
        state_id: int,
        book_id: int,
        forecast_at: str,
        model_version: str,
        probability_a: float,
        market_midpoint_a: float,
        edge_a: float,
        edge_b: float,
        best_side: Optional[str],
        breakdown: Dict[str, Any],
    ) -> int:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO forecasts(
                    match_id, state_id, book_id, forecast_at, model_version,
                    probability_a, market_midpoint_a, edge_a, edge_b,
                    best_side, breakdown_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    state_id,
                    book_id,
                    forecast_at,
                    model_version,
                    probability_a,
                    market_midpoint_a,
                    edge_a,
                    edge_b,
                    best_side,
                    _json(breakdown),
                ),
            )
            row = connection.execute(
                """
                SELECT forecast_id FROM forecasts
                WHERE state_id=? AND book_id=? AND model_version=?
                """,
                (state_id, book_id, model_version),
            ).fetchone()
        return int(row["forecast_id"])

    def ensure_account(self, name: str, initial_cash: float = 1000.0) -> int:
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        now = isoformat(utc_now())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_accounts(name, initial_cash, cash, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (name, initial_cash, initial_cash, now, now),
            )
            row = connection.execute(
                "SELECT account_id FROM paper_accounts WHERE name=?", (name,)
            ).fetchone()
        return int(row["account_id"])

    def resolve_match(self, match_id: str, winner: str, resolved_at: str) -> None:
        if winner not in ("A", "B"):
            raise ValueError("winner must be A or B")
        resolved = canonical_timestamp(resolved_at)
        with self.connect() as connection:
            latest = connection.execute(
                "SELECT max(forecast_at) FROM forecasts WHERE match_id=?", (match_id,)
            ).fetchone()[0]
            if latest and parse_timestamp(resolved) < parse_timestamp(latest):
                raise ValueError("resolved_at cannot be earlier than the latest forecast")
            connection.execute(
                """
                UPDATE matches
                SET status='resolved', winner=?, resolved_at=?, live=0, ended=1,
                    finished_observed_at=COALESCE(finished_observed_at, ?), updated_at=?
                WHERE match_id=?
                """,
                (winner, resolved, resolved, resolved, match_id),
            )

    def update_match_lifecycle(
        self, match_id: str, live: bool, ended: bool, observed_at: Optional[str] = None
    ) -> bool:
        """Refresh an existing fixture state without inserting stale history.

        ``ended`` is sticky: a provider regression cannot reopen a finished
        fixture or make it paper-tradable again. Returns true only for the first
        transition into the finished state.
        """
        observed = canonical_timestamp(observed_at or isoformat(utc_now()))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status, ended FROM matches WHERE match_id=?", (match_id,)
            ).fetchone()
            if row is None:
                return False
            is_ended = bool(row["ended"]) or bool(ended) or row["status"] != "open"
            became_ended = not bool(row["ended"]) and is_ended
            is_live = bool(live) and not is_ended and row["status"] == "open"
            connection.execute(
                """
                UPDATE matches
                SET live=?, ended=?,
                    finished_observed_at=CASE
                        WHEN ?=1 THEN COALESCE(finished_observed_at, ?)
                        ELSE finished_observed_at
                    END,
                    updated_at=?
                WHERE match_id=?
                """,
                (
                    1 if is_live else 0,
                    1 if is_ended else 0,
                    1 if is_ended else 0,
                    observed,
                    observed,
                    match_id,
                ),
            )
        return became_ended

    def upsert_discovered_match(
        self, record: Dict[str, Any], default_prior_a: float
    ) -> str:
        """Insert or refresh a Gamma-discovered match.

        The prior is written only on first insert. Later prior changes must go
        through ``apply_prior`` so every revision is recorded in ``llm_priors``
        rather than silently overwritten.

        Returns "inserted", "updated", or "conflict" (identity fields moved,
        which means the slug was reused for a different pairing).
        """
        now = isoformat(utc_now())
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM matches WHERE match_id=?", (record["match_id"],)
            ).fetchone()
            if existing is None:
                match = Match(
                    match_id=record["match_id"],
                    team_a=record["team_a"],
                    team_b=record["team_b"],
                    best_of=record["best_of"],
                    prior_probability_a=default_prior_a,
                    token_a=record["token_a"],
                    token_b=record["token_b"],
                    source="polymarket-gamma",
                    external_id=record.get("event_id", ""),
                    scheduled_at=record.get("scheduled_at"),
                ).validated()
                connection.execute(
                    """
                    INSERT INTO matches(
                        match_id, team_a, team_b, best_of, prior_probability_a,
                        token_a, token_b, source, external_id, scheduled_at,
                        created_at, league, serie, tournament, title, context,
                        provider_match_id, condition_id, live, liquidity,
                        prior_source, updated_at, ended, finished_observed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        match.match_id,
                        match.team_a,
                        match.team_b,
                        match.best_of,
                        match.prior_probability_a,
                        match.token_a,
                        match.token_b,
                        match.source,
                        match.external_id,
                        match.scheduled_at,
                        now,
                        record.get("league", ""),
                        record.get("serie", ""),
                        record.get("tournament", ""),
                        record.get("title", ""),
                        record.get("context", ""),
                        str(record.get("pandascore_match_id") or ""),
                        record.get("condition_id", ""),
                        1 if record.get("live") and not record.get("ended") else 0,
                        float(record.get("liquidity") or 0.0),
                        "seed",
                        now,
                        1 if record.get("ended") else 0,
                        now if record.get("ended") else None,
                    ),
                )
                return "inserted"
            identity_changed = (
                existing["team_a"] != record["team_a"]
                or existing["team_b"] != record["team_b"]
                or existing["token_a"] != record["token_a"]
                or existing["token_b"] != record["token_b"]
            )
            if identity_changed:
                return "conflict"
            is_ended = bool(existing["ended"]) or bool(record.get("ended"))
            is_live = (
                bool(record.get("live"))
                and not is_ended
                and existing["status"] == "open"
            )
            connection.execute(
                """
                UPDATE matches
                SET league=?, serie=?, tournament=?, title=?, context=?,
                    provider_match_id=?, condition_id=?, live=?, liquidity=?,
                    scheduled_at=COALESCE(?, scheduled_at), updated_at=?, ended=?,
                    finished_observed_at=CASE
                        WHEN ?=1 THEN COALESCE(finished_observed_at, ?)
                        ELSE finished_observed_at
                    END
                WHERE match_id=?
                """,
                (
                    record.get("league", ""),
                    record.get("serie", ""),
                    record.get("tournament", ""),
                    record.get("title", ""),
                    record.get("context", ""),
                    str(record.get("pandascore_match_id") or ""),
                    record.get("condition_id", ""),
                    1 if is_live else 0,
                    float(record.get("liquidity") or 0.0),
                    record.get("scheduled_at"),
                    now,
                    1 if is_ended else 0,
                    1 if is_ended else 0,
                    now,
                    record["match_id"],
                ),
            )
            return "updated"

    def open_matches(self, only_live: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM matches WHERE status='open'"
        if only_live:
            query += " AND live=1"
        query += " ORDER BY COALESCE(scheduled_at, created_at) ASC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def matches_needing_prior(
        self, limit: int = 10, min_liquidity: float = 0.0
    ) -> List[Dict[str, Any]]:
        """Un-priced open matches, soonest to start first.

        Live and ended matches are excluded: a pre-match prior arriving after
        the first round has been played is worse than useless, because the
        engine would treat stale information as a clean starting point.

        Ordering is by kick-off, not by liquidity. Liquidity ranking looked
        sensible and quietly failed: a match starting in ten minutes lost its
        slot to a bigger one starting tomorrow, went live unpriced, and spent
        its whole broadcast showing the seed prior. Cheap API priors made
        throughput, not budget, the binding constraint, so the right question
        is "what is about to start" and liquidity is only a floor.

        Matches whose start time has already passed are excluded outright.
        Sorting soonest-first without that filter puts yesterday's fixtures at
        the head of the queue and spends the budget writing pre-match forecasts
        for matches that are already over. A small grace window absorbs the
        routine schedule slippage in esports.
        """
        cutoff = isoformat(utc_now() - timedelta(minutes=GRACE_MINUTES))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.* FROM matches m
                WHERE m.status='open'
                  AND m.live=0
                  AND m.prior_source='seed'
                  AND m.liquidity >= ?
                  AND m.scheduled_at IS NOT NULL
                  AND m.scheduled_at >= ?
                ORDER BY m.scheduled_at ASC, m.liquidity DESC
                LIMIT ?
                """,
                (float(min_liquidity), cutoff, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_prior(
        self,
        match_id: str,
        parsed: Dict[str, Any],
        provider: str,
        model: str,
        prompt_sha256: str = "",
        backend: str = "",
    ) -> int:
        """Record an LLM prior and promote it onto the match, atomically."""
        usage = parsed.get("usage") or {}
        probability = float(parsed["probability_team_a"])
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO llm_priors(
                    match_id, created_at, evidence_cutoff_at, provider, model,
                    prompt_version, probability_a, raw_probability_a, confidence,
                    reasoning_summary, key_factors_json, evidence_json,
                    assumptions_json, usage_json, estimated_cost_usd,
                    prompt_sha256, backend, applied
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    match_id,
                    isoformat(utc_now()),
                    parsed.get("evidence_cutoff_at", ""),
                    provider,
                    model,
                    parsed.get("prompt_version", ""),
                    probability,
                    float(parsed.get("raw_probability_team_a", probability)),
                    parsed.get("confidence", "low"),
                    parsed.get("reasoning_summary", ""),
                    _json(parsed.get("key_factors") or []),
                    _json(parsed.get("supporting_evidence") or []),
                    _json(parsed.get("assumptions") or []),
                    _json(usage),
                    float(usage.get("estimated_cost_usd", 0.0) or 0.0),
                    prompt_sha256,
                    backend or provider,
                ),
            )
            connection.execute(
                """
                UPDATE matches
                SET prior_probability_a=?, prior_source=?, updated_at=?
                WHERE match_id=?
                """,
                (probability, "llm:%s" % model, isoformat(utc_now()), match_id),
            )
            return int(cursor.lastrowid)

    def latest_prior(self, match_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM llm_priors WHERE match_id=?
                ORDER BY created_at DESC, prior_id DESC LIMIT 1
                """,
                (match_id,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ("key_factors", "evidence", "assumptions", "usage"):
            raw = value.pop("%s_json" % key, None)
            value[key] = json.loads(raw) if raw else ([] if key != "usage" else {})
        return value

    def count_priors_since(self, since: str) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM llm_priors WHERE created_at >= ?", (since,)
                ).fetchone()[0]
            )

    def prior_cost_since(self, since: str) -> float:
        with self.connect() as connection:
            return float(
                connection.execute(
                    """
                    SELECT COALESCE(sum(estimated_cost_usd), 0)
                    FROM llm_priors WHERE created_at >= ?
                    """,
                    (since,),
                ).fetchone()[0]
            )

    def start_collector_run(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO collector_runs(started_at) VALUES(?)",
                (isoformat(utc_now()),),
            )
            return int(cursor.lastrowid)

    def finish_collector_run(
        self,
        run_id: int,
        status: str,
        discovered: int,
        ticked: int,
        skipped: int,
        errors: List[str],
        notices: Optional[List[str]] = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE collector_runs
                SET finished_at=?, status=?, discovered=?, ticked=?, skipped=?,
                    errors_json=?, notices_json=?
                WHERE run_id=?
                """,
                (
                    isoformat(utc_now()),
                    status,
                    int(discovered),
                    int(ticked),
                    int(skipped),
                    _json(errors[:50]),
                    _json((notices or [])[:20]),
                    int(run_id),
                ),
            )

    def latest_collector_run(self) -> Optional[Dict[str, Any]]:
        """The most recent *finished* run, falling back to one in flight.

        Reporting the in-flight run makes the dashboard flip to "0 ticked,
        running" for a second on every cycle, which reads as the feed breaking.
        """
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM collector_runs
                ORDER BY (finished_at IS NULL), run_id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["errors"] = json.loads(value.pop("errors_json") or "[]")
        value["notices"] = json.loads(value.pop("notices_json", "[]") or "[]")
        return value

    def void_match(self, match_id: str, resolved_at: str) -> None:
        """Close a match that settled without a winner (cancelled, forfeited)."""
        resolved = canonical_timestamp(resolved_at)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE matches
                SET status='void', winner=NULL, resolved_at=?, live=0, ended=1,
                    finished_observed_at=COALESCE(finished_observed_at, ?), updated_at=?
                WHERE match_id=?
                """,
                (resolved, resolved, resolved, match_id),
            )

    def matches_awaiting_resolution(
        self, min_age_hours: float = 1.0, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Open matches whose scheduled start is far enough in the past.

        Ordered oldest first so a long backlog drains deterministically rather
        than re-checking the same recent fixtures every cycle.
        """
        cutoff = isoformat(utc_now() - timedelta(hours=float(min_age_hours)))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM matches
                WHERE status='open'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at < ?
                -- Confirmed-finished fixtures must not sit behind an old
                -- unsettled backlog. Current ended matches settle first.
                ORDER BY ended DESC, scheduled_at DESC
                LIMIT ?
                """,
                (cutoff, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_prior_market_probability(self, prior_id: int, probability: float) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE llm_priors SET market_probability_a=? WHERE prior_id=?",
                (float(probability), int(prior_id)),
            )

    def nearest_market_probability(
        self, match_id: str, at: str
    ) -> Optional[float]:
        """Latest book midpoint recorded at or before ``at``, else ``None``.

        Strictly backward-looking. A snapshot taken *after* the prior has seen
        news the AI had not, so using one would hand the market a look ahead
        and quietly measure the wrong thing. Priors written before this
        baseline was captured are dropped from scoring instead.
        """
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT bid_a, ask_a FROM market_snapshots
                WHERE match_id=? AND source_at <= ?
                ORDER BY source_at DESC LIMIT 1
                """,
                (match_id, at),
            ).fetchone()
        if row is None:
            return None
        return (float(row["bid_a"]) + float(row["ask_a"])) / 2.0

    def scoring_rows(self) -> List[Dict[str, Any]]:
        """Resolved matches that carry an LLM prior, with the market baseline.

        Only matches with a real prior are scored: a seed prior is the absence
        of a view, and scoring it as if it were an AI forecast would flatter or
        damn the model with predictions it never made.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.match_id, m.team_a, m.team_b, m.winner, m.resolved_at,
                    m.scheduled_at, m.liquidity,
                    p.prior_id, p.probability_a AS ai_probability_a,
                    p.market_probability_a, p.created_at AS prior_created_at,
                    p.confidence, p.model,
                    f.probability_a AS final_probability_a,
                    f.market_midpoint_a AS final_market_a
                FROM matches m
                JOIN llm_priors p ON p.prior_id = (
                    SELECT p2.prior_id FROM llm_priors p2
                    WHERE p2.match_id = m.match_id
                    ORDER BY p2.created_at ASC, p2.prior_id ASC LIMIT 1
                )
                LEFT JOIN forecasts f ON f.forecast_id = (
                    SELECT f2.forecast_id FROM forecasts f2
                    WHERE f2.match_id = m.match_id
                    ORDER BY f2.forecast_at DESC, f2.forecast_id DESC LIMIT 1
                )
                WHERE m.status='resolved' AND m.winner IS NOT NULL
                ORDER BY m.resolved_at DESC
                """
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            if value.get("market_probability_a") is None:
                value["market_probability_a"] = self.nearest_market_probability(
                    value["match_id"], value["prior_created_at"]
                )
            result.append(value)
        return result

    def latest_rows(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.*,
                    f.forecast_id, f.forecast_at, f.model_version,
                    f.probability_a, f.market_midpoint_a, f.edge_a, f.edge_b,
                    f.best_side, f.breakdown_json,
                    s.source_at AS state_source_at, s.observed_at AS state_observed_at,
                    s.maps_a, s.maps_b, s.rounds_a, s.rounds_b, s.current_map,
                    s.side_advantage_a, s.economy_a, s.economy_b, s.map_bias_a,
                    b.source_at AS book_source_at, b.observed_at AS book_observed_at,
                    b.bid_a, b.ask_a, b.bid_b, b.ask_b,
                    p.probability_a AS prior_probability_llm,
                    p.confidence AS prior_confidence,
                    p.reasoning_summary AS prior_reasoning,
                    p.model AS prior_model,
                    p.created_at AS prior_created_at,
                    p.key_factors_json, p.evidence_json, p.assumptions_json
                FROM matches m
                LEFT JOIN forecasts f ON f.forecast_id = (
                    SELECT f2.forecast_id FROM forecasts f2
                    WHERE f2.match_id=m.match_id
                    ORDER BY f2.forecast_at DESC, f2.forecast_id DESC LIMIT 1
                )
                LEFT JOIN state_snapshots s ON s.state_id=f.state_id
                LEFT JOIN market_snapshots b ON b.book_id=f.book_id
                LEFT JOIN llm_priors p ON p.prior_id = (
                    SELECT p2.prior_id FROM llm_priors p2
                    WHERE p2.match_id=m.match_id
                    ORDER BY p2.created_at DESC, p2.prior_id DESC LIMIT 1
                )
                -- Stable ordering. Sorting by the latest forecast time made
                -- the row order change on every tick, which the dashboard then
                -- rendered as cards jumping around once a minute.
                ORDER BY m.live DESC,
                         COALESCE(m.scheduled_at, m.created_at) ASC,
                         m.match_id ASC
                """
            ).fetchall()
        result: List[Dict[str, Any]] = []
        for item in rows:
            value = dict(item)
            if value.get("breakdown_json"):
                value["breakdown"] = json.loads(value.pop("breakdown_json"))
            else:
                value.pop("breakdown_json", None)
            for key in ("key_factors", "evidence", "assumptions"):
                raw = value.pop("%s_json" % key, None)
                value[key] = json.loads(raw) if raw else []
            value.pop("usage_json", None)
            value.pop("raw_response", None)
            # Long free text is not needed by the dashboard list view.
            value.pop("context", None)
            result.append(value)
        return result

    def match_detail(
        self, match_id: str, account_name: str = "live-paper", history_limit: int = 400
    ) -> Optional[Dict[str, Any]]:
        """Everything known about one match, for its own page.

        The forecast trajectory is the part that has no home on the overview:
        it is the only view of how the model and the market moved against each
        other while the series was actually being played.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM matches WHERE match_id=?", (match_id,)
            ).fetchone()
            if row is None:
                return None
            detail: Dict[str, Any] = dict(row)

            history = connection.execute(
                """
                SELECT
                    f.forecast_at, f.probability_a, f.market_midpoint_a,
                    f.edge_a, f.edge_b, f.best_side, f.model_version,
                    s.maps_a, s.maps_b, s.rounds_a, s.rounds_b, s.current_map,
                    s.source AS state_source, s.source_at AS state_source_at,
                    b.bid_a, b.ask_a, b.bid_b, b.ask_b
                FROM forecasts f
                LEFT JOIN state_snapshots s ON s.state_id = f.state_id
                LEFT JOIN market_snapshots b ON b.book_id = f.book_id
                WHERE f.match_id = ?
                ORDER BY f.forecast_at ASC, f.forecast_id ASC
                LIMIT ?
                """,
                (match_id, int(history_limit)),
            ).fetchall()
            detail["history"] = [dict(item) for item in history]

            account = connection.execute(
                "SELECT account_id FROM paper_accounts WHERE name=?", (account_name,)
            ).fetchone()
            positions: List[Dict[str, Any]] = []
            trades: List[Dict[str, Any]] = []
            if account is not None:
                positions = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT * FROM paper_positions
                        WHERE account_id=? AND match_id=?
                        """,
                        (account["account_id"], match_id),
                    ).fetchall()
                ]
                trades = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT * FROM paper_trades
                        WHERE account_id=? AND match_id=?
                        ORDER BY trade_id DESC LIMIT 50
                        """,
                        (account["account_id"], match_id),
                    ).fetchall()
                ]
            detail["positions"] = positions
            detail["trades"] = trades

        prior = self.latest_prior(match_id)
        if prior is not None and prior.get("market_probability_a") is None:
            # Same backward-looking fallback the scorer uses, so the page and
            # the scoreboard cannot disagree about whether this is scoreable.
            prior["market_probability_a"] = self.nearest_market_probability(
                match_id, prior["created_at"]
            )
        detail["prior"] = prior
        latest = detail["history"][-1] if detail["history"] else None
        detail["latest"] = latest
        detail["generated_at"] = isoformat(utc_now())
        return detail

    def account_payload(self, name: str = "live-paper") -> Dict[str, Any]:
        with self.connect() as connection:
            account = connection.execute(
                "SELECT * FROM paper_accounts WHERE name=?", (name,)
            ).fetchone()
            if account is None:
                return {}
            positions = connection.execute(
                """
                SELECT p.*, m.team_a, m.team_b,
                       b.bid_a, b.ask_a, b.bid_b, b.ask_b
                FROM paper_positions p
                JOIN matches m ON m.match_id=p.match_id
                LEFT JOIN market_snapshots b ON b.book_id=(
                    SELECT b2.book_id FROM market_snapshots b2
                    WHERE b2.match_id=p.match_id ORDER BY b2.source_at DESC, b2.book_id DESC LIMIT 1
                )
                WHERE p.account_id=? AND p.shares>0
                ORDER BY p.updated_at DESC
                """,
                (account["account_id"],),
            ).fetchall()
            trades = connection.execute(
                """
                SELECT t.*, m.team_a, m.team_b
                FROM paper_trades t JOIN matches m ON m.match_id=t.match_id
                WHERE t.account_id=? ORDER BY t.trade_id DESC LIMIT 100
                """,
                (account["account_id"],),
            ).fetchall()
            realized_row = connection.execute(
                """
                SELECT COALESCE(sum(realized_pnl), 0) AS realized_pnl
                FROM paper_positions WHERE account_id=?
                """,
                (account["account_id"],),
            ).fetchone()

        position_values: List[Dict[str, Any]] = []
        mark_value = 0.0
        for position in positions:
            value = dict(position)
            bid = value.get("bid_a") if value["outcome"] == "A" else value.get("bid_b")
            marked = float(value["shares"]) * float(bid or 0.0)
            value["mark_value"] = marked
            mark_value += marked
            position_values.append(value)
        account_dict = dict(account)
        account_dict["positions"] = position_values
        account_dict["trades"] = [dict(item) for item in trades]
        account_dict["mark_value"] = mark_value
        account_dict["realized_pnl"] = float(realized_row["realized_pnl"])
        account_dict["equity"] = float(account["cash"]) + mark_value
        account_dict["return"] = (
            account_dict["equity"] / float(account["initial_cash"])
        ) - 1.0
        return account_dict

    def dashboard_payload(self, account_name: str = "live-paper") -> Dict[str, Any]:
        with self.connect() as connection:
            counts = {
                "matches": connection.execute("SELECT count(*) FROM matches").fetchone()[0],
                "live": connection.execute(
                    "SELECT count(*) FROM matches WHERE live=1 AND ended=0 AND status='open'"
                ).fetchone()[0],
                "pending": connection.execute(
                    "SELECT count(*) FROM matches WHERE ended=1 AND status='open'"
                ).fetchone()[0],
                "priced": connection.execute(
                    "SELECT count(*) FROM matches WHERE prior_source != 'seed'"
                ).fetchone()[0],
                "states": connection.execute("SELECT count(*) FROM state_snapshots").fetchone()[0],
                "books": connection.execute("SELECT count(*) FROM market_snapshots").fetchone()[0],
                "forecasts": connection.execute("SELECT count(*) FROM forecasts").fetchone()[0],
                "priors": connection.execute("SELECT count(*) FROM llm_priors").fetchone()[0],
                "trades": connection.execute("SELECT count(*) FROM paper_trades").fetchone()[0],
                "resolved": connection.execute(
                    "SELECT count(*) FROM matches WHERE status='resolved'"
                ).fetchone()[0],
            }
            latest = connection.execute(
                "SELECT max(forecast_at) FROM forecasts"
            ).fetchone()[0]
        return {
            "generated_at": isoformat(utc_now()),
            "counts": counts,
            "latest_forecast_at": latest,
            "collector": self.latest_collector_run(),
            "scoring": self.scoring_summary(),
            "matches": self.latest_rows(),
            "account": self.account_payload(account_name),
        }

    def scoring_summary(self) -> Dict[str, Any]:
        """Scoreboard for the dashboard. Imported lazily to keep storage leaf-level."""
        from .scoring import score

        report = score(self)
        report["matches"] = report["matches"][:20]
        return report
