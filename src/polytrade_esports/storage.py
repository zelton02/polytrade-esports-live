import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import timedelta
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .timeutil import canonical_timestamp, isoformat, parse_timestamp, utc_now
from .types import BookQuote, LiveState, Match
from .state_guard import STRATEGIES, strategy_for_state, validate_strategy


EXECUTION_MODES = ("legacy", "depth-sim")


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
    strategy TEXT NOT NULL DEFAULT 'pre-match',
    paper_enabled INTEGER NOT NULL DEFAULT 0,
    entry_enabled INTEGER NOT NULL DEFAULT 0,
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
    entry_strategy TEXT NOT NULL DEFAULT 'pre-match',
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
    decision_strategy TEXT NOT NULL DEFAULT 'pre-match',
    entry_strategy TEXT NOT NULL DEFAULT 'pre-match',
    reason TEXT NOT NULL,
    traded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trade_account_time
ON paper_trades(account_id, traded_at);

CREATE TABLE IF NOT EXISTS state_rejections (
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    source TEXT NOT NULL,
    source_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_json TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    rejected_at TEXT NOT NULL,
    UNIQUE(match_id, source, source_at, reason, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_state_rejection_match_time
ON state_rejections(match_id, rejected_at);
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

-- Liquipedia facts are cached because action=parse is rate limited to one
-- request per 30 seconds; refetching per forecast would make pricing a slate
-- of matches take hours.
CREATE TABLE IF NOT EXISTS team_facts (
    team_name TEXT PRIMARY KEY,
    page TEXT,
    fetched_at TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
"""

# Final paper-execution ledger.  The old paper_trades table remains as the
# compact position/PnL journal and is explicitly labelled ``legacy`` during
# migration.  New decisions first become orders, then one or more immutable
# fills; only those fills are materialized into paper_trades and positions.
SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS order_book_levels (
    level_id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES market_snapshots(book_id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (outcome IN ('A', 'B')),
    side TEXT NOT NULL CHECK (side IN ('BID', 'ASK')),
    level_index INTEGER NOT NULL,
    price REAL NOT NULL CHECK (price > 0 AND price < 1),
    size REAL NOT NULL CHECK (size > 0),
    UNIQUE(book_id, outcome, side, level_index)
);

CREATE INDEX IF NOT EXISTS idx_book_levels_book
ON order_book_levels(book_id, outcome, side, level_index);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL REFERENCES paper_accounts(account_id),
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    forecast_id INTEGER NOT NULL REFERENCES forecasts(forecast_id),
    execution_mode TEXT NOT NULL DEFAULT 'depth-sim'
        CHECK (execution_mode IN ('depth-sim')),
    decision_strategy TEXT NOT NULL DEFAULT 'pre-match',
    entry_strategy TEXT NOT NULL DEFAULT 'pre-match',
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    outcome TEXT NOT NULL CHECK (outcome IN ('A', 'B')),
    order_type TEXT NOT NULL DEFAULT 'IOC' CHECK (order_type IN ('IOC')),
    requested_shares REAL NOT NULL CHECK (requested_shares > 0),
    signal_price REAL NOT NULL CHECK (signal_price > 0 AND signal_price < 1),
    limit_price REAL NOT NULL CHECK (limit_price > 0 AND limit_price < 1),
    signal_at TEXT NOT NULL,
    execute_after TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN (
            'PENDING', 'SUBMITTED', 'PARTIALLY_FILLED', 'FILLED',
            'REJECTED', 'EXPIRED', 'CANCELLED'
        )),
    filled_shares REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL NOT NULL DEFAULT 0,
    fee_paid REAL NOT NULL DEFAULT 0,
    cash_delta REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    execution_book_id INTEGER REFERENCES market_snapshots(book_id),
    attempts INTEGER NOT NULL DEFAULT 0,
    rejection_reason TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_orders_due
ON paper_orders(status, execute_after, order_id);

CREATE INDEX IF NOT EXISTS idx_paper_orders_account_match
ON paper_orders(account_id, match_id, order_id);

CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES paper_orders(order_id),
    book_id INTEGER NOT NULL REFERENCES market_snapshots(book_id),
    level_index INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    outcome TEXT NOT NULL CHECK (outcome IN ('A', 'B')),
    shares REAL NOT NULL CHECK (shares > 0),
    price REAL NOT NULL CHECK (price > 0 AND price < 1),
    notional REAL NOT NULL CHECK (notional > 0),
    fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
    liquidity TEXT NOT NULL DEFAULT 'TAKER' CHECK (liquidity IN ('TAKER')),
    filled_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_fills_order
ON paper_fills(order_id, fill_id);

CREATE TABLE IF NOT EXISTS execution_controls (
    account_id INTEGER PRIMARY KEY REFERENCES paper_accounts(account_id),
    kill_switch INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS executor_status (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
    worker_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'starting',
    last_heartbeat_at TEXT,
    last_order_at TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    filled INTEGER NOT NULL DEFAULT 0,
    partial INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '{}'
);
"""

# Shadow research has a deliberately separate ledger.  Nothing in the paper
# engine joins these tables, and the CHECK constraints make the non-applied
# boundary inspectable in the database rather than merely a caller convention.
# CREATE IF NOT EXISTS keeps this compatible with every prior SQLite version;
# the coordinated release owns the metadata schema-version bump.
SCHEMA_SHADOW_PANEL = """
CREATE TABLE IF NOT EXISTS shadow_panel_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL REFERENCES matches(match_id),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    evidence_cutoff_at TEXT NOT NULL,
    panel_version TEXT NOT NULL,
    consensus_method TEXT NOT NULL DEFAULT 'median-with-mad-band-v1',
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    backend TEXT NOT NULL,
    grounded_teams INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'partial', 'failed')),
    successful_members INTEGER NOT NULL DEFAULT 0,
    consensus_probability_a REAL
        CHECK (consensus_probability_a IS NULL OR
               (consensus_probability_a > 0 AND consensus_probability_a < 1)),
    uncertainty_low_a REAL,
    uncertainty_high_a REAL,
    probability_spread REAL,
    probability_mad REAL,
    market_probability_a REAL,
    market_captured_at TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]',
    -- Captured at run time because matches.liquidity drifts: comparing the
    -- panel against the market is only decision-relevant on books deep enough
    -- to trade, so the analysis has to stratify on the depth we actually saw.
    liquidity_at_run REAL,
    applied INTEGER NOT NULL DEFAULT 0 CHECK (applied = 0),
    UNIQUE(match_id, panel_version, model, backend)
);

CREATE INDEX IF NOT EXISTS idx_shadow_panel_runs_time
ON shadow_panel_runs(created_at, match_id);

CREATE TABLE IF NOT EXISTS shadow_panel_members (
    member_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES shadow_panel_runs(run_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    prompt_sha256 TEXT NOT NULL,
    probability_a REAL
        CHECK (probability_a IS NULL OR (probability_a > 0 AND probability_a < 1)),
    raw_probability_a REAL,
    confidence TEXT CHECK (
        confidence IS NULL OR confidence IN ('low', 'medium', 'high')
    ),
    reasoning_summary TEXT NOT NULL DEFAULT '',
    key_factors_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    raw_response TEXT NOT NULL DEFAULT '',
    usage_json TEXT NOT NULL DEFAULT '{}',
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    applied INTEGER NOT NULL DEFAULT 0 CHECK (applied = 0),
    UNIQUE(run_id, role)
);

CREATE INDEX IF NOT EXISTS idx_shadow_panel_members_run
ON shadow_panel_members(run_id, member_id);
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
    # "sound", or a reason the prior must not be scored. Kept as a column
    # rather than a deletion: how the method failed is itself a finding.
    ("methodology", "TEXT NOT NULL DEFAULT 'sound'"),
    # The exact facts block the model was given. Stored so a reader can check
    # the reasoning against its inputs instead of taking the summary on trust.
    ("verified_facts", "TEXT NOT NULL DEFAULT ''"),
    ("grounded_teams", "INTEGER NOT NULL DEFAULT 0"),
)

SHADOW_RUN_COLUMNS = (
    ("liquidity_at_run", "REAL"),
)

COLLECTOR_COLUMNS = (
    # Expected provider limitations are not failures, but hiding them makes a
    # maps-only feed look like a round-level feed. Keep them with every run so
    # the dashboard can state the effective capability honestly.
    ("notices_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("feed_status_json", "TEXT NOT NULL DEFAULT '{}'"),
)

BOOK_COLUMNS = (
    ("depth_available", "INTEGER NOT NULL DEFAULT 0"),
)

FORECAST_COLUMNS = (
    ("strategy", "TEXT NOT NULL DEFAULT 'pre-match'"),
    # Added after strategy attribution shipped. Legacy forecasts cannot be
    # proven eligible for the grounded paper cohort, so they default false and
    # are excluded from decision counts instead of inflating them.
    ("paper_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("entry_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("execution_mode", "TEXT NOT NULL DEFAULT 'legacy'"),
)

POSITION_COLUMNS = (
    ("entry_strategy", "TEXT NOT NULL DEFAULT 'pre-match'"),
    ("execution_mode", "TEXT NOT NULL DEFAULT 'legacy'"),
    ("fees_paid", "REAL NOT NULL DEFAULT 0"),
)

TRADE_COLUMNS = (
    ("decision_strategy", "TEXT NOT NULL DEFAULT 'pre-match'"),
    ("entry_strategy", "TEXT NOT NULL DEFAULT 'pre-match'"),
    ("execution_mode", "TEXT NOT NULL DEFAULT 'legacy'"),
    ("order_id", "INTEGER"),
    ("fee", "REAL NOT NULL DEFAULT 0"),
    ("slippage", "REAL NOT NULL DEFAULT 0"),
    ("signal_price", "REAL"),
    ("fill_latency_ms", "REAL NOT NULL DEFAULT 0"),
)

# Esports start times slip routinely; a prior written just after the scheduled
# time is still a pre-match prior in practice.
GRACE_MINUTES = 20

# Marks the first generation of API priors, which had no evidence to reason
# from. See _invalidate_ungrounded_priors.
REASON_UNGROUNDED = "ungrounded: no web access, no verified facts; the only input was the market own summary"

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
    # When pricing was last abandoned for want of verifiable facts. A team with
    # no wiki page still has none an hour later, so without this the same
    # unpriceable fixtures reclaim the queue every cycle and starve the ones
    # that could actually be priced.
    ("prior_skipped_at", "TEXT"),
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
            connection.executescript(SCHEMA_V6)
            connection.executescript(SCHEMA_SHADOW_PANEL)
            version_row = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            previous_version = int(version_row["value"]) if version_row else 0
            self._migrate_columns(connection, "matches", MATCH_COLUMNS)
            self._migrate_columns(connection, "llm_priors", PRIOR_COLUMNS)
            self._migrate_columns(connection, "collector_runs", COLLECTOR_COLUMNS)
            self._migrate_columns(connection, "market_snapshots", BOOK_COLUMNS)
            self._migrate_columns(connection, "forecasts", FORECAST_COLUMNS)
            self._migrate_columns(connection, "paper_positions", POSITION_COLUMNS)
            self._migrate_columns(connection, "paper_trades", TRADE_COLUMNS)
            self._migrate_columns(
                connection, "shadow_panel_runs", SHADOW_RUN_COLUMNS
            )
            connection.execute(
                """
                UPDATE matches
                SET live=0, ended=1,
                    finished_observed_at=COALESCE(finished_observed_at, resolved_at)
                WHERE status IN ('resolved', 'void')
                """
            )
            # These rewrite rows rather than change the schema, and each is
            # idempotent, so re-running them is cheap and safe. Gating them
            # behind an applied-repairs ledger is the right long-term shape but
            # changes migration semantics (a fresh database would mark them
            # done immediately), so it belongs in its own change with its own
            # tests rather than at the end of this one.
            self._label_legacy_backends(connection)
            self._invalidate_ungrounded_priors(connection)
            self._backfill_grounding(connection)
            self._backfill_web_grounding(connection)
            if previous_version < 4:
                self._backfill_strategy_attribution(connection)
            repair = connection.execute(
                "SELECT value FROM metadata "
                "WHERE key='backfill_maps_only_degraded_v1'"
            ).fetchone()
            if repair is None:
                self._backfill_maps_only_degraded_attribution(connection)
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES(?, ?)",
                    ("backfill_maps_only_degraded_v1", "complete"),
                )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                ("schema_version", "8"),
            )

    @staticmethod
    def _backfill_strategy_attribution(connection: sqlite3.Connection) -> None:
        """Classify rows written before strategy attribution existed."""
        connection.execute(
            """
            UPDATE forecasts
            SET strategy = CASE
                WHEN EXISTS (
                    SELECT 1 FROM state_snapshots s
                    WHERE s.state_id=forecasts.state_id
                      AND (
                          s.source='polymarket-sports-ws'
                          OR (s.source='pandascore' AND (s.rounds_a + s.rounds_b) > 0)
                      )
                ) THEN 'round-live'
                WHEN EXISTS (
                    SELECT 1 FROM state_snapshots s
                    WHERE s.state_id=forecasts.state_id
                      AND (
                          s.maps_a + s.maps_b > 0
                          OR s.source LIKE '%-maps'
                          OR s.source='canonical-frozen'
                      )
                ) THEN 'map-boundary'
                WHEN EXISTS (
                    SELECT 1 FROM matches m
                    WHERE m.match_id=forecasts.match_id
                      AND m.scheduled_at IS NOT NULL
                      AND datetime(forecasts.forecast_at)
                          <= datetime(m.scheduled_at, '+20 minutes')
                ) THEN 'pre-match'
                ELSE 'map-boundary'
            END
            """
        )
        connection.execute(
            """
            UPDATE paper_trades
            SET decision_strategy = COALESCE(
                    (SELECT f.strategy FROM forecasts f
                     WHERE f.forecast_id=paper_trades.forecast_id),
                    'pre-match'
                ),
                entry_strategy = COALESCE(
                    (SELECT f.strategy FROM forecasts f
                     WHERE f.forecast_id=paper_trades.forecast_id),
                    'pre-match'
                )
            """
        )
        connection.execute(
            """
            UPDATE paper_positions
            SET entry_strategy = COALESCE((
                SELECT t.entry_strategy FROM paper_trades t
                WHERE t.account_id=paper_positions.account_id
                  AND t.match_id=paper_positions.match_id
                  AND t.outcome=paper_positions.outcome
                  AND t.action='BUY'
                ORDER BY t.trade_id DESC LIMIT 1
            ), 'pre-match')
            """
        )

    @staticmethod
    def _backfill_maps_only_degraded_attribution(
        connection: sqlite3.Connection,
    ) -> None:
        """Split provably degraded depth-sim rows from real map transitions.

        Version 6 called every no-round live observation ``map-boundary``.  A
        blind SQL rewrite based only on the provider name would also relabel a
        real transition delivered by that provider.  Replay the recorded
        forecast states in order and reuse the live classifier instead.  Only
        the depth-sim cohort is touched: older legacy forecasts were never
        proven paper-eligible and are excluded from the strategy dashboard.
        """
        rows = connection.execute(
            """
            SELECT f.forecast_id, f.match_id, f.forecast_at, f.strategy,
                   s.source_at, s.observed_at, s.maps_a, s.maps_b,
                   s.rounds_a, s.rounds_b, s.current_map,
                   s.side_advantage_a, s.economy_a, s.economy_b,
                   s.map_bias_a, s.source, s.raw_json
            FROM forecasts f
            JOIN state_snapshots s ON s.state_id=f.state_id
            WHERE f.execution_mode='depth-sim'
            ORDER BY f.match_id, f.forecast_at, f.forecast_id
            """
        ).fetchall()
        previous_by_match: Dict[str, LiveState] = {}
        rewritten: List[int] = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            state = LiveState(
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
                raw=raw,
            )
            previous = previous_by_match.get(str(row["match_id"]))
            guard = raw.get("canonical_guard") or {}
            degraded_source = (
                str(row["source"] or "").endswith("-maps")
                or raw.get("round_detail_available") is False
                or guard.get("reason") == "round_detail_unavailable"
            )
            if (
                row["strategy"] == "map-boundary"
                # Without an earlier depth-sim forecast there is no recorded
                # comparison that can disprove a real map transition. Keep
                # that first observation untouched rather than guessing.
                and previous is not None
                and degraded_source
                and strategy_for_state(True, previous, state, False)
                    == "maps-only-degraded"
            ):
                rewritten.append(int(row["forecast_id"]))
            previous_by_match[str(row["match_id"])] = state

        for forecast_id in rewritten:
            connection.execute(
                "UPDATE forecasts SET strategy='maps-only-degraded' "
                "WHERE forecast_id=?",
                (forecast_id,),
            )
            # Decision attribution follows the forecast. Entry attribution
            # deliberately does not: degraded observations can close a
            # position, but cannot become the strategy that opened it.
            connection.execute(
                "UPDATE paper_orders SET decision_strategy='maps-only-degraded' "
                "WHERE forecast_id=? AND execution_mode='depth-sim'",
                (forecast_id,),
            )
            connection.execute(
                "UPDATE paper_trades SET decision_strategy='maps-only-degraded' "
                "WHERE forecast_id=? AND execution_mode='depth-sim'",
                (forecast_id,),
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
    def _backfill_web_grounding(connection: sqlite3.Connection) -> None:
        """Credit web-researched priors that predate the grounding column.

        A prior carrying cited sources was researched: the no-tool prompt
        forbids producing evidence at all, so a non-empty evidence list can
        only have come from a backend that actually looked the matchup up.
        Both teams are credited because the research covered the fixture, not
        one side of it.
        """
        connection.execute(
            """
            UPDATE llm_priors SET grounded_teams = 2
            WHERE grounded_teams = 0
              AND methodology = 'sound'
              AND evidence_json <> '[]'
            """
        )

    @staticmethod
    def _backfill_grounding(connection: sqlite3.Connection) -> None:
        """Mark priors that predate the grounded_teams column.

        Sound inference rather than a guess: the grounded prompt version only
        ever ran with require_facts on, and that path skips the match outright
        when neither team can be grounded. A stored prior of that version
        therefore had at least one grounded team. One is recorded rather than
        two, because which it was is no longer knowable.
        """
        connection.execute(
            """
            UPDATE llm_priors SET grounded_teams = 1
            WHERE grounded_teams = 0
              AND methodology = 'sound'
              AND prompt_version LIKE 'cs2-prior-v2%'
            """
        )

    @staticmethod
    def _invalidate_ungrounded_priors(connection: sqlite3.Connection) -> None:
        """Exclude priors that had no independent evidence to reason from.

        The early API-backed priors ran with no web access, a training cutoff
        26 months before the matches, and nothing in the prompt but the
        market machine-written summary. A controlled test showed the output
        simply tracked that summary: removing it moved the estimate to 0.46,
        and reversing it moved the estimate from 0.27 to 0.70. Those are
        paraphrases of the market, not forecasts of it, so scoring them as an
        independent view would measure nothing.

        The rows stay; only their eligibility for scoring changes.
        """
        connection.execute(
            """
            UPDATE llm_priors SET methodology = ?
            WHERE methodology = 'sound'
              AND backend = 'deepseek'
              AND prompt_version = 'cs2-prior-v1'
            """,
            (REASON_UNGROUNDED,),
        )
        # Invalidating the prior is only half the job. The match still carries
        # the discredited probability and still reads as priced, so it never
        # re-enters the pricing queue and the bad number stays on the board.
        # Reset any match whose only priors are unsound back to the seed.
        connection.execute(
            """
            UPDATE matches
            SET prior_probability_a = 0.5, prior_source = 'seed'
            WHERE prior_source <> 'seed'
              AND EXISTS (
                  SELECT 1 FROM llm_priors p
                  WHERE p.match_id = matches.match_id
                    AND p.methodology <> 'sound'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM llm_priors p
                  WHERE p.match_id = matches.match_id
                    AND p.methodology = 'sound'
              )
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

    def record_state_rejection(
        self,
        previous: LiveState,
        candidate: LiveState,
        reason: str,
    ) -> int:
        """Persist a provider transition that the canonical guard refused."""
        old = previous.normalized()
        new = candidate.normalized()
        if old.match_id != new.match_id:
            raise ValueError("rejected states must reference one match")
        payload = new.to_dict()
        # Fingerprint the transition content rather than its timestamps/raw
        # envelope. A stale Gamma fallback can repeat the same 0-0 regression
        # every minute; the cycle counter should keep reporting that, but the
        # audit table only needs one row until either side's trusted state moves.
        digest = _sha(
            {
                "reason": str(reason),
                "previous": {
                    "maps_a": old.maps_a,
                    "maps_b": old.maps_b,
                    "rounds_a": old.rounds_a,
                    "rounds_b": old.rounds_b,
                    "current_map": old.current_map,
                    "source": old.source,
                },
                "candidate": {
                    "maps_a": new.maps_a,
                    "maps_b": new.maps_b,
                    "rounds_a": new.rounds_a,
                    "rounds_b": new.rounds_b,
                    "current_map": new.current_map,
                    "source": new.source,
                },
            }
        )
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT rejection_id FROM state_rejections
                WHERE match_id=? AND reason=? AND payload_sha256=?
                ORDER BY rejection_id DESC LIMIT 1
                """,
                (new.match_id, str(reason), digest),
            ).fetchone()
            if existing is not None:
                return int(existing["rejection_id"])
            connection.execute(
                """
                INSERT OR IGNORE INTO state_rejections(
                    match_id, source, source_at, observed_at, reason,
                    previous_json, candidate_json, payload_sha256, rejected_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new.match_id,
                    new.source,
                    new.source_at,
                    new.observed_at,
                    str(reason),
                    _json(old.to_dict()),
                    _json(payload),
                    digest,
                    isoformat(utc_now()),
                ),
            )
            row = connection.execute(
                """
                SELECT rejection_id FROM state_rejections
                WHERE match_id=? AND source=? AND source_at=?
                  AND reason=? AND payload_sha256=?
                """,
                (new.match_id, new.source, new.source_at, str(reason), digest),
            ).fetchone()
        return int(row["rejection_id"])

    def state_rejection_summary(self, limit: int = 20) -> Dict[str, Any]:
        with self.connect() as connection:
            total = int(
                connection.execute("SELECT count(*) FROM state_rejections").fetchone()[0]
            )
            recent = connection.execute(
                """
                SELECT rejection_id, match_id, source, source_at, observed_at,
                       reason, rejected_at
                FROM state_rejections
                ORDER BY rejection_id DESC LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        return {"total": total, "recent": [dict(row) for row in recent]}

    def record_book(self, quote: BookQuote, store_depth: bool = False) -> int:
        normalized = quote.normalized()
        payload = normalized.to_dict()
        digest = _sha(payload)
        depth = {
            (outcome, side): normalized.levels(outcome, side)
            for outcome in ("A", "B")
            for side in ("bids", "asks")
        }
        depth_available = all(depth.values())
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
            book_id = int(row["book_id"])
            # Forecast snapshots retain the provider payload for audit, but
            # normalizing every level on every one-minute pricing tick would
            # multiply the database by thousands of rows an hour.  The exact
            # depth consumed by an execution is normalized here when the
            # executor records its post-latency book.
            if store_depth:
                for (outcome, side), levels in depth.items():
                    for index, level in enumerate(levels):
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO order_book_levels(
                                book_id, outcome, side, level_index, price, size
                            ) VALUES(?, ?, ?, ?, ?, ?)
                            """,
                            (
                                book_id,
                                outcome,
                                "BID" if side == "bids" else "ASK",
                                index,
                                level.price,
                                level.size,
                            ),
                        )
            if store_depth and depth_available:
                connection.execute(
                    "UPDATE market_snapshots SET depth_available=1 WHERE book_id=?",
                    (book_id,),
                )
        return book_id

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
        strategy: str = "pre-match",
        paper_enabled: bool = False,
        entry_enabled: bool = False,
        execution_mode: str = "depth-sim",
    ) -> int:
        strategy = validate_strategy(strategy)
        if execution_mode not in EXECUTION_MODES:
            raise ValueError("invalid execution_mode: %s" % execution_mode)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO forecasts(
                    match_id, state_id, book_id, forecast_at, model_version,
                    probability_a, market_midpoint_a, edge_a, edge_b,
                    best_side, breakdown_json, strategy, paper_enabled,
                    entry_enabled, execution_mode
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    strategy,
                    int(bool(paper_enabled)),
                    int(bool(entry_enabled)),
                    execution_mode,
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

    def create_paper_order(
        self,
        *,
        client_order_id: str,
        account_id: int,
        match_id: str,
        forecast_id: int,
        decision_strategy: str,
        entry_strategy: str,
        action: str,
        outcome: str,
        requested_shares: float,
        signal_price: float,
        limit_price: float,
        signal_at: str,
        execute_after: str,
        expires_at: str,
        reason: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        decision_strategy = validate_strategy(decision_strategy)
        entry_strategy = validate_strategy(entry_strategy)
        action = str(action).upper()
        outcome = str(outcome).upper()
        if action not in ("BUY", "SELL"):
            raise ValueError("paper order action must be BUY or SELL")
        if outcome not in ("A", "B"):
            raise ValueError("paper order outcome must be A or B")
        shares = float(requested_shares)
        signal = float(signal_price)
        limit = float(limit_price)
        if shares <= 0.0:
            raise ValueError("paper order requested_shares must be positive")
        if not 0.0 < signal < 1.0 or not 0.0 < limit < 1.0:
            raise ValueError("paper order prices must be in (0, 1)")
        signalled = canonical_timestamp(signal_at)
        due = canonical_timestamp(execute_after)
        expiry = canonical_timestamp(expires_at)
        if parse_timestamp(due) < parse_timestamp(signalled):
            raise ValueError("execute_after cannot precede signal_at")
        if parse_timestamp(expiry) < parse_timestamp(due):
            raise ValueError("expires_at cannot precede execute_after")
        now = isoformat(utc_now())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_orders(
                    client_order_id, account_id, match_id, forecast_id,
                    decision_strategy, entry_strategy, action, outcome,
                    requested_shares, signal_price, limit_price, signal_at,
                    execute_after, expires_at, reason, config_json,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_order_id,
                    int(account_id),
                    match_id,
                    int(forecast_id),
                    decision_strategy,
                    entry_strategy,
                    action,
                    outcome,
                    shares,
                    signal,
                    limit,
                    signalled,
                    due,
                    expiry,
                    str(reason),
                    _json(config),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM paper_orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
        return dict(row)

    def cancel_pending_orders(
        self,
        account_id: int,
        match_id: str,
        reason: str = "superseded",
        except_forecast_id: Optional[int] = None,
    ) -> int:
        now = isoformat(utc_now())
        with self.connect() as connection:
            if except_forecast_id is None:
                cursor = connection.execute(
                    """
                    UPDATE paper_orders
                    SET status='CANCELLED', rejection_reason=?, completed_at=?, updated_at=?
                    WHERE account_id=? AND match_id=? AND status='PENDING'
                    """,
                    (str(reason), now, now, int(account_id), match_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE paper_orders
                    SET status='CANCELLED', rejection_reason=?, completed_at=?, updated_at=?
                    WHERE account_id=? AND match_id=? AND status='PENDING'
                      AND forecast_id<>?
                    """,
                    (
                        str(reason), now, now, int(account_id), match_id,
                        int(except_forecast_id),
                    ),
                )
        return int(cursor.rowcount)

    def due_order_ids(
        self, account_name: str, due_at: str, limit: int = 50
    ) -> List[int]:
        due = canonical_timestamp(due_at)
        with self.connect() as connection:
            account = connection.execute(
                "SELECT account_id FROM paper_accounts WHERE name=?", (account_name,)
            ).fetchone()
            if account is None:
                return []
            connection.execute(
                """
                UPDATE paper_orders
                SET status='EXPIRED', rejection_reason='order_ttl_elapsed',
                    completed_at=?, updated_at=?
                WHERE account_id=? AND status='PENDING' AND expires_at < ?
                """,
                (due, due, int(account["account_id"]), due),
            )
            rows = connection.execute(
                """
                SELECT order_id FROM paper_orders
                WHERE account_id=? AND status='PENDING' AND execute_after <= ?
                ORDER BY execute_after, order_id LIMIT ?
                """,
                (int(account["account_id"]), due, max(0, int(limit))),
            ).fetchall()
        return [int(row["order_id"]) for row in rows]

    def claim_order(self, order_id: int, submitted_at: str) -> Optional[Dict[str, Any]]:
        submitted = canonical_timestamp(submitted_at)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE paper_orders
                SET status='SUBMITTED', submitted_at=?, attempts=attempts+1, updated_at=?
                WHERE order_id=? AND status='PENDING'
                  AND execute_after <= ? AND expires_at >= ?
                """,
                (submitted, submitted, int(order_id), submitted, submitted),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT o.*, a.name AS account_name, a.initial_cash, a.cash,
                       m.token_a, m.token_b, m.status AS match_status,
                       m.live AS match_live, m.ended AS match_ended,
                       f.entry_enabled AS forecast_entry_enabled
                FROM paper_orders o
                JOIN paper_accounts a ON a.account_id=o.account_id
                JOIN matches m ON m.match_id=o.match_id
                JOIN forecasts f ON f.forecast_id=o.forecast_id
                WHERE o.order_id=?
                """,
                (int(order_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def retry_order(
        self, order_id: int, execute_after: str, error: str
    ) -> None:
        due = canonical_timestamp(execute_after)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_orders
                SET status='PENDING', execute_after=?, submitted_at=NULL,
                    last_error=?, updated_at=?
                WHERE order_id=? AND status='SUBMITTED'
                """,
                (due, str(error)[:500], isoformat(utc_now()), int(order_id)),
            )

    def reject_order(self, order_id: int, reason: str, completed_at: str) -> None:
        completed = canonical_timestamp(completed_at)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_orders
                SET status='REJECTED', rejection_reason=?, completed_at=?, updated_at=?
                WHERE order_id=? AND status IN ('PENDING', 'SUBMITTED')
                """,
                (str(reason), completed, completed, int(order_id)),
            )

    def execution_kill_switch(self, account_id: int) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM execution_controls WHERE account_id=?",
                (int(account_id),),
            ).fetchone()
        return dict(row) if row is not None else {
            "account_id": int(account_id), "kill_switch": 0, "reason": ""
        }

    def set_execution_kill_switch(
        self, account_name: str, enabled: bool, reason: str = ""
    ) -> Dict[str, Any]:
        account_id = self.ensure_account(account_name)
        now = isoformat(utc_now())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_controls(account_id, kill_switch, reason, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    kill_switch=excluded.kill_switch,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (account_id, int(bool(enabled)), str(reason), now),
            )
        return self.execution_kill_switch(account_id)

    def paper_accounts_for_match(self, match_id: str) -> List[str]:
        """Accounts whose live orders or positions need terminal handling."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.name
                FROM paper_accounts a
                JOIN paper_positions p ON p.account_id=a.account_id
                WHERE p.match_id=? AND p.shares>0
                UNION
                SELECT a.name
                FROM paper_accounts a
                JOIN paper_orders o ON o.account_id=a.account_id
                WHERE o.match_id=? AND o.status IN ('PENDING','SUBMITTED')
                ORDER BY name
                """,
                (match_id, match_id),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def update_executor_status(
        self,
        *,
        worker_id: str,
        status: str,
        processed_delta: int = 0,
        filled_delta: int = 0,
        partial_delta: int = 0,
        rejected_delta: int = 0,
        errors_delta: int = 0,
        last_error: str = "",
        touched_order: bool = False,
    ) -> None:
        now = isoformat(utc_now())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO executor_status(
                    singleton, worker_id, status, last_heartbeat_at, last_order_at,
                    processed, filled, partial, rejected, errors, last_error
                ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    worker_id=excluded.worker_id,
                    status=excluded.status,
                    last_heartbeat_at=excluded.last_heartbeat_at,
                    last_order_at=CASE
                        WHEN excluded.last_order_at IS NOT NULL THEN excluded.last_order_at
                        ELSE executor_status.last_order_at
                    END,
                    processed=executor_status.processed+excluded.processed,
                    filled=executor_status.filled+excluded.filled,
                    partial=executor_status.partial+excluded.partial,
                    rejected=executor_status.rejected+excluded.rejected,
                    errors=executor_status.errors+excluded.errors,
                    last_error=CASE
                        WHEN excluded.last_error<>'' THEN excluded.last_error
                        ELSE executor_status.last_error
                    END
                """,
                (
                    str(worker_id), str(status), now, now if touched_order else None,
                    int(processed_delta), int(filled_delta), int(partial_delta),
                    int(rejected_delta), int(errors_delta), str(last_error)[:500],
                ),
            )

    def executor_status(self) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM executor_status WHERE singleton=1"
            ).fetchone()
        return dict(row) if row is not None else {}

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
        # prior_grounded_teams rides along because the paper engine needs it:
        # a forecast about a fixture where one side is unknown is not a view
        # about that fixture, and must not size a position.
        query = (
            "SELECT m.*, ("
            "  SELECT p.grounded_teams FROM llm_priors p"
            "  WHERE p.match_id = m.match_id AND p.methodology = 'sound'"
            "  ORDER BY p.created_at DESC, p.prior_id DESC LIMIT 1"
            ") AS prior_grounded_teams "
            "FROM matches m WHERE m.status='open'"
        )
        if only_live:
            query += " AND m.live=1"
        query += " ORDER BY COALESCE(m.scheduled_at, m.created_at) ASC"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query).fetchall()]

    def matches_needing_prior(
        self,
        limit: int = 10,
        min_liquidity: float = 0.0,
        skip_backoff_hours: float = 6.0,
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
        retry_after = isoformat(
            utc_now() - timedelta(hours=float(skip_backoff_hours))
        )
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
                  AND (m.prior_skipped_at IS NULL OR m.prior_skipped_at < ?)
                ORDER BY m.scheduled_at ASC, m.liquidity DESC
                LIMIT ?
                """,
                (float(min_liquidity), cutoff, retry_after, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_prior_skipped(self, match_id: str) -> None:
        """Record that pricing was abandoned for lack of verifiable facts."""
        with self.connect() as connection:
            connection.execute(
                "UPDATE matches SET prior_skipped_at=? WHERE match_id=?",
                (isoformat(utc_now()), match_id),
            )

    def apply_prior(
        self,
        match_id: str,
        parsed: Dict[str, Any],
        provider: str,
        model: str,
        prompt_sha256: str = "",
        backend: str = "",
        verified_facts: str = "",
        grounded_teams: int = 0,
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
                    prompt_sha256, backend, verified_facts, grounded_teams, applied
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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
                    verified_facts,
                    int(grounded_teams),
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

    def matches_needing_shadow_panel(
        self,
        panel_version: str,
        model: str,
        backend: str,
        limit: int = 10,
        min_liquidity: float = 0.0,
        min_lead_minutes: float = 10.0,
    ) -> List[Dict[str, Any]]:
        """Return upcoming fixtures that have not entered this shadow cohort.

        This selection is independent of ``prior_source``: the experiment is
        meant to run beside either a seed or a production LLM prior.  It never
        changes the production pricing queue.
        """
        if float(min_lead_minutes) < 0.0:
            raise ValueError("shadow panel min_lead_minutes cannot be negative")
        # Four independent calls can take minutes. Unlike the production prior
        # queue, the shadow cohort gets no schedule grace: every member must
        # finish from an indisputably pre-match information set.
        cutoff = isoformat(
            utc_now() + timedelta(minutes=float(min_lead_minutes))
        )
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.* FROM matches m
                WHERE m.status='open'
                  AND m.live=0
                  AND m.ended=0
                  AND m.liquidity >= ?
                  AND m.scheduled_at IS NOT NULL
                  AND m.scheduled_at >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM shadow_panel_runs r
                      WHERE r.match_id=m.match_id
                        AND r.panel_version=?
                        AND r.model=?
                        AND r.backend=?
                  )
                ORDER BY m.scheduled_at ASC, m.liquidity DESC
                LIMIT ?
                """,
                (
                    float(min_liquidity),
                    cutoff,
                    panel_version,
                    model,
                    backend,
                    int(limit),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def shadow_panel_for_match(self, match_id: str) -> List[Dict[str, Any]]:
        """Panel runs for one match, newest first, each with its members.

        Normally one row. A second appears only when the worker's model changed
        while the fixture was still upcoming, which re-opens the per-model
        queue -- so the caller must render a list, not the first row.
        """
        with self.connect() as connection:
            runs = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT run_id, model, status, successful_members,
                           consensus_probability_a, market_probability_a,
                           probability_spread, created_at
                    FROM shadow_panel_runs
                    WHERE match_id=? AND consensus_probability_a IS NOT NULL
                    ORDER BY created_at DESC, run_id DESC
                    """,
                    (match_id,),
                ).fetchall()
            ]
            if not runs:
                return []
            identifiers = [int(run["run_id"]) for run in runs]
            placeholders = ",".join("?" * len(identifiers))
            members = connection.execute(
                """
                SELECT run_id, role, probability_a, confidence
                FROM shadow_panel_members
                WHERE status='completed' AND probability_a IS NOT NULL
                  AND run_id IN (%s)
                ORDER BY member_id ASC
                """ % placeholders,
                identifiers,
            ).fetchall()
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for member in members:
            grouped.setdefault(int(member["run_id"]), []).append(dict(member))
        for run in runs:
            run["members"] = grouped.get(int(run["run_id"]), [])
        return runs

    def shadow_scoring_rows(self) -> List[Dict[str, Any]]:
        """Resolved matches that carry a shadow consensus and its baseline.

        ``liquidity_at_run`` is preferred over the match's current liquidity,
        which drifts after the panel ran; older rows predate that column and
        fall back, so ``liquidity_is_current`` marks which reading was used.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    r.run_id, r.match_id, r.model, r.backend, r.panel_version,
                    r.status, r.successful_members,
                    r.consensus_probability_a, r.market_probability_a,
                    r.probability_spread,
                    COALESCE(r.liquidity_at_run, m.liquidity) AS liquidity,
                    r.liquidity_at_run IS NULL AS liquidity_is_current,
                    m.team_a, m.team_b, m.winner, m.resolved_at
                FROM shadow_panel_runs r
                JOIN matches m ON m.match_id = r.match_id
                WHERE m.status='resolved' AND m.winner IS NOT NULL
                  AND r.consensus_probability_a IS NOT NULL
                  AND r.market_probability_a IS NOT NULL
                ORDER BY m.resolved_at ASC, r.run_id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def shadow_member_rows(self) -> List[Dict[str, Any]]:
        """Every successful member probability, for single-vs-median analysis."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, role, probability_a
                FROM shadow_panel_members
                WHERE status='completed' AND probability_a IS NOT NULL
                ORDER BY run_id ASC, member_id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def shadow_run_counts(self) -> Dict[str, int]:
        with self.connect() as connection:
            total = connection.execute(
                "SELECT count(*) FROM shadow_panel_runs"
            ).fetchone()[0]
            scored = connection.execute(
                """
                SELECT count(*) FROM shadow_panel_runs r
                JOIN matches m ON m.match_id = r.match_id
                WHERE m.status='resolved' AND m.winner IS NOT NULL
                  AND r.consensus_probability_a IS NOT NULL
                  AND r.market_probability_a IS NOT NULL
                """
            ).fetchone()[0]
        return {"total": int(total), "scored": int(scored),
                "awaiting": int(total) - int(scored)}

    def begin_shadow_panel_run(
        self,
        match_id: str,
        evidence_cutoff_at: str,
        panel_version: str,
        provider: str,
        model: str,
        backend: str,
        grounded_teams: int,
        liquidity: Optional[float] = None,
    ) -> Optional[int]:
        """Reserve one panel cohort, returning ``None`` on a concurrent claim."""
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO shadow_panel_runs(
                    match_id, created_at, evidence_cutoff_at, panel_version,
                    provider, model, backend, grounded_teams,
                    liquidity_at_run, applied
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    match_id,
                    isoformat(utc_now()),
                    evidence_cutoff_at,
                    panel_version,
                    provider,
                    model,
                    backend,
                    int(grounded_teams),
                    None if liquidity is None else float(liquidity),
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def record_shadow_panel_member(
        self,
        run_id: int,
        role: str,
        prompt_sha256: str,
        parsed: Dict[str, Any],
    ) -> int:
        usage = parsed.get("usage") or {}
        probability = float(parsed["probability_team_a"])
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shadow_panel_members(
                    run_id, role, created_at, status, prompt_sha256,
                    probability_a, raw_probability_a, confidence,
                    reasoning_summary, key_factors_json, evidence_json,
                    assumptions_json, raw_response, usage_json,
                    estimated_cost_usd, error, applied
                ) VALUES(?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0)
                """,
                (
                    int(run_id),
                    role,
                    isoformat(utc_now()),
                    prompt_sha256,
                    probability,
                    float(parsed.get("raw_probability_team_a", probability)),
                    parsed.get("confidence", "low"),
                    parsed.get("reasoning_summary", ""),
                    _json(parsed.get("key_factors") or []),
                    _json(parsed.get("supporting_evidence") or []),
                    _json(parsed.get("assumptions") or []),
                    str(parsed.get("raw_response") or ""),
                    _json(usage),
                    float(usage.get("estimated_cost_usd", 0.0) or 0.0),
                ),
            )
            return int(cursor.lastrowid)

    def record_shadow_panel_member_error(
        self,
        run_id: int,
        role: str,
        prompt_sha256: str,
        error: str,
        usage: Dict[str, Any],
        raw_response: str = "",
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO shadow_panel_members(
                    run_id, role, created_at, status, prompt_sha256,
                    raw_response, usage_json, estimated_cost_usd, error, applied
                ) VALUES(?, ?, ?, 'failed', ?, ?, ?, ?, ?, 0)
                """,
                (
                    int(run_id),
                    role,
                    isoformat(utc_now()),
                    prompt_sha256,
                    str(raw_response or ""),
                    _json(usage),
                    float(usage.get("estimated_cost_usd", 0.0) or 0.0),
                    str(error)[:4000],
                ),
            )
            return int(cursor.lastrowid)

    def finish_shadow_panel_run(
        self,
        run_id: int,
        status: str,
        consensus: Optional[Dict[str, float]],
        market_probability_a: Optional[float],
        market_captured_at: Optional[str],
        usage: Dict[str, Any],
        errors: List[str],
    ) -> None:
        if status not in ("completed", "partial", "failed"):
            raise ValueError("invalid shadow panel status")
        value = consensus or {}
        with self.connect() as connection:
            successful = int(
                connection.execute(
                    """
                    SELECT count(*) FROM shadow_panel_members
                    WHERE run_id=? AND status='completed'
                    """,
                    (int(run_id),),
                ).fetchone()[0]
            )
            connection.execute(
                """
                UPDATE shadow_panel_runs
                SET completed_at=?, status=?, successful_members=?,
                    consensus_probability_a=?, uncertainty_low_a=?,
                    uncertainty_high_a=?, probability_spread=?, probability_mad=?,
                    market_probability_a=?, market_captured_at=?, usage_json=?,
                    estimated_cost_usd=?, errors_json=?, applied=0
                WHERE run_id=?
                """,
                (
                    isoformat(utc_now()),
                    status,
                    successful,
                    value.get("probability_a"),
                    value.get("uncertainty_low_a"),
                    value.get("uncertainty_high_a"),
                    value.get("spread"),
                    value.get("mad"),
                    market_probability_a,
                    market_captured_at,
                    _json(usage),
                    float(usage.get("estimated_cost_usd", 0.0) or 0.0),
                    _json(errors),
                    int(run_id),
                ),
            )

    def count_shadow_panel_runs_since(self, since: str) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM shadow_panel_runs WHERE created_at >= ?",
                    (since,),
                ).fetchone()[0]
            )

    def shadow_panel_cost_since(self, since: str) -> float:
        with self.connect() as connection:
            return float(
                connection.execute(
                    """
                    SELECT COALESCE(sum(estimated_cost_usd), 0)
                    FROM shadow_panel_runs WHERE created_at >= ?
                    """,
                    (since,),
                ).fetchone()[0]
            )

    def latest_shadow_panel_run(self, match_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM shadow_panel_runs WHERE match_id=?
                ORDER BY created_at DESC, run_id DESC LIMIT 1
                """,
                (match_id,),
            ).fetchone()
            if row is None:
                return None
            members = connection.execute(
                """
                SELECT * FROM shadow_panel_members
                WHERE run_id=? ORDER BY member_id
                """,
                (int(row["run_id"]),),
            ).fetchall()
        result = dict(row)
        result["usage"] = json.loads(result.pop("usage_json") or "{}")
        result["errors"] = json.loads(result.pop("errors_json") or "[]")
        result["members"] = []
        for member_row in members:
            member = dict(member_row)
            member["key_factors"] = json.loads(member.pop("key_factors_json") or "[]")
            member["evidence"] = json.loads(member.pop("evidence_json") or "[]")
            member["assumptions"] = json.loads(member.pop("assumptions_json") or "[]")
            member["usage"] = json.loads(member.pop("usage_json") or "{}")
            result["members"].append(member)
        return result

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
        feed_status: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE collector_runs
                SET finished_at=?, status=?, discovered=?, ticked=?, skipped=?,
                    errors_json=?, notices_json=?, feed_status_json=?
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
                    _json(feed_status or {}),
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
        value["feed"] = json.loads(value.pop("feed_status_json", "{}") or "{}")
        return value

    def void_match(self, match_id: str, resolved_at: str) -> None:
        """Close a match that settled without a winner (cancelled, forfeited)."""
        resolved = canonical_timestamp(resolved_at)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE paper_orders
                SET status='CANCELLED', rejection_reason='match_void',
                    completed_at=?, updated_at=?
                WHERE match_id=? AND status IN ('PENDING','SUBMITTED')
                """,
                (resolved, resolved, match_id),
            )
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
        """Open matches that are finished or whose start is far in the past.

        Ordered oldest first so a long backlog drains deterministically rather
        than re-checking the same recent fixtures every cycle. Confirmed-ended
        fixtures bypass the age gate: the gate exists to avoid polling every
        merely-started match, not to delay a result Gamma already knows.
        """
        cutoff = isoformat(utc_now() - timedelta(hours=float(min_age_hours)))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM matches
                WHERE status='open'
                  AND (
                    ended=1
                    OR (scheduled_at IS NOT NULL AND scheduled_at < ?)
                  )
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
                      AND p2.methodology = 'sound'
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
                    p.backend AS prior_backend,
                    p.grounded_teams AS prior_grounded_teams,
                    p.created_at AS prior_created_at,
                    p.key_factors_json, p.evidence_json, p.assumptions_json
                FROM matches m
                LEFT JOIN forecasts f ON f.forecast_id = (
                    SELECT f2.forecast_id FROM forecasts f2
                    WHERE f2.match_id=m.match_id
                    ORDER BY f2.forecast_at DESC, f2.forecast_id DESC LIMIT 1
                )
                LEFT JOIN state_snapshots s ON s.state_id = (
                    SELECT s2.state_id FROM state_snapshots s2
                    WHERE s2.match_id=m.match_id
                    ORDER BY s2.source_at DESC, s2.state_id DESC LIMIT 1
                )
                LEFT JOIN market_snapshots b ON b.book_id=f.book_id
                LEFT JOIN llm_priors p ON p.prior_id = (
                    SELECT p2.prior_id FROM llm_priors p2
                    WHERE p2.match_id=m.match_id AND p2.methodology='sound'
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
                    f.edge_a, f.edge_b, f.best_side, f.model_version, f.strategy,
                    f.execution_mode,
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

            latest_state = connection.execute(
                """
                SELECT maps_a, maps_b, rounds_a, rounds_b, current_map,
                       source AS state_source, source_at AS state_source_at
                FROM state_snapshots
                WHERE match_id=?
                ORDER BY source_at DESC, state_id DESC LIMIT 1
                """,
                (match_id,),
            ).fetchone()

            account = connection.execute(
                "SELECT account_id FROM paper_accounts WHERE name=?", (account_name,)
            ).fetchone()
            positions: List[Dict[str, Any]] = []
            trades: List[Dict[str, Any]] = []
            orders: List[Dict[str, Any]] = []
            fills: List[Dict[str, Any]] = []
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
                orders = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT * FROM paper_orders
                        WHERE account_id=? AND match_id=?
                        ORDER BY order_id DESC LIMIT 50
                        """,
                        (account["account_id"], match_id),
                    ).fetchall()
                ]
                fills = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT pf.*, po.decision_strategy, po.entry_strategy,
                               po.signal_price, po.requested_shares
                        FROM paper_fills pf
                        JOIN paper_orders po ON po.order_id=pf.order_id
                        WHERE po.account_id=? AND po.match_id=?
                        ORDER BY pf.fill_id DESC LIMIT 100
                        """,
                        (account["account_id"], match_id),
                    ).fetchall()
                ]
            detail["positions"] = positions
            detail["trades"] = trades
            detail["orders"] = orders
            detail["fills"] = fills

        prior = self.latest_prior(match_id)
        if prior is not None and prior.get("market_probability_a") is None:
            # Same backward-looking fallback the scorer uses, so the page and
            # the scoreboard cannot disagree about whether this is scoreable.
            prior["market_probability_a"] = self.nearest_market_probability(
                match_id, prior["created_at"]
            )
        detail["prior"] = prior
        detail["shadow"] = self.shadow_panel_for_match(match_id)
        latest = dict(detail["history"][-1]) if detail["history"] else {}
        if latest_state is not None:
            latest.update(dict(latest_state))
        if not latest:
            latest = None
        detail["latest"] = latest
        detail["generated_at"] = isoformat(utc_now())
        return detail

    def cached_team_facts(
        self, team_name: str, max_age_hours: float = 12.0
    ) -> Optional[Dict[str, Any]]:
        cutoff = isoformat(utc_now() - timedelta(hours=float(max_age_hours)))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM team_facts WHERE team_name=? AND fetched_at >= ?",
                (team_name, cutoff),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["facts_json"])
        value["cached_at"] = row["fetched_at"]
        return value

    def store_team_facts(self, team_name: str, facts: Dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO team_facts(team_name, page, fetched_at, facts_json, error)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(team_name) DO UPDATE SET
                    page=excluded.page,
                    fetched_at=excluded.fetched_at,
                    facts_json=excluded.facts_json,
                    error=excluded.error
                """,
                (
                    team_name,
                    facts.get("page") or "",
                    isoformat(utc_now()),
                    _json(facts),
                    str(facts.get("error") or ""),
                ),
            )

    def market_at_last_state_change(
        self, match_id: str, state: Any
    ) -> Optional[float]:
        """Market midpoint when the state last actually changed.

        Anchoring on ``state_id`` does not work: every tick stores a new state
        row because ``source_at`` moves, so the "first forecast for this state"
        is always the one just written and the drift is always zero. The
        comparison has to be on the state's *content* -- maps and rounds --
        which is the only thing that moves the model.
        """
        with self.connect() as connection:
            changed_at = connection.execute(
                """
                SELECT max(f.forecast_at)
                FROM forecasts f JOIN state_snapshots s ON s.state_id = f.state_id
                WHERE f.match_id = ?
                  AND (s.maps_a <> ? OR s.maps_b <> ?
                       OR s.rounds_a <> ? OR s.rounds_b <> ?)
                """,
                (match_id, state.maps_a, state.maps_b, state.rounds_a, state.rounds_b),
            ).fetchone()[0]
            row = connection.execute(
                """
                SELECT f.market_midpoint_a
                FROM forecasts f
                WHERE f.match_id = ? AND f.forecast_at > COALESCE(?, '')
                ORDER BY f.forecast_at ASC, f.forecast_id ASC LIMIT 1
                """,
                (match_id, changed_at),
            ).fetchone()
        return None if row is None else float(row["market_midpoint_a"])

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
        account_dict["strategies"] = self.strategy_summary(name)
        return account_dict

    def strategy_summary(self, account_name: str = "live-paper") -> List[Dict[str, Any]]:
        """Depth-sim activity funnel and PnL attribution by information horizon.

        Forecast eligibility is global because forecasts are shared model
        observations. Signals onward are account-specific. ``decisions`` is a
        compatibility alias for ``paper_enabled``; the dashboard uses the
        explicit funnel names so a forecast, an order and a fill are never
        presented as though they were the same event.
        """
        summary: Dict[str, Dict[str, Any]] = {
            strategy: {
                "strategy": strategy,
                "scope": "depth-sim",
                "forecasts": 0,
                "paper_enabled": 0,
                "entry_enabled": 0,
                "signals": 0,
                "orders": 0,
                "fills": 0,
                # Kept for API compatibility with the pre-funnel dashboard.
                "decisions": 0,
                "trades": 0,
                "buys": 0,
                "sells": 0,
                "settles": 0,
                "realized_pnl": 0.0,
                "open_positions": 0,
                "open_cost": 0.0,
                "mark_value": 0.0,
                "unrealized_pnl": 0.0,
                "total_pnl": 0.0,
            }
            for strategy in STRATEGIES
        }
        with self.connect() as connection:
            for row in connection.execute(
                """
                SELECT strategy,
                       count(*) AS forecasts,
                       sum(CASE WHEN paper_enabled=1 THEN 1 ELSE 0 END)
                           AS paper_enabled,
                       sum(CASE WHEN entry_enabled=1 THEN 1 ELSE 0 END)
                           AS entry_enabled
                FROM forecasts
                WHERE execution_mode='depth-sim'
                GROUP BY strategy
                """
            ).fetchall():
                if row["strategy"] in summary:
                    item = summary[row["strategy"]]
                    for key in ("forecasts", "paper_enabled", "entry_enabled"):
                        item[key] = int(row[key] or 0)
                    item["decisions"] = item["paper_enabled"]

            account = connection.execute(
                "SELECT account_id FROM paper_accounts WHERE name=?", (account_name,)
            ).fetchone()
            if account is None:
                return [summary[strategy] for strategy in STRATEGIES]
            account_id = int(account["account_id"])

            for row in connection.execute(
                """
                SELECT decision_strategy,
                       count(DISTINCT forecast_id) AS signals,
                       count(*) AS orders
                FROM paper_orders
                WHERE account_id=? AND execution_mode='depth-sim'
                GROUP BY decision_strategy
                """,
                (account_id,),
            ).fetchall():
                item = summary.get(row["decision_strategy"])
                if item is not None:
                    item["signals"] = int(row["signals"] or 0)
                    item["orders"] = int(row["orders"] or 0)

            for row in connection.execute(
                """
                SELECT o.decision_strategy, count(*) AS fills
                FROM paper_fills pf
                JOIN paper_orders o ON o.order_id=pf.order_id
                WHERE o.account_id=? AND o.execution_mode='depth-sim'
                GROUP BY o.decision_strategy
                """,
                (account_id,),
            ).fetchall():
                item = summary.get(row["decision_strategy"])
                if item is not None:
                    item["fills"] = int(row["fills"] or 0)

            for row in connection.execute(
                """
                SELECT decision_strategy,
                       count(*) AS trades,
                       sum(CASE WHEN action='BUY' THEN 1 ELSE 0 END) AS buys,
                       sum(CASE WHEN action='SELL' THEN 1 ELSE 0 END) AS sells,
                       sum(CASE WHEN action='SETTLE' THEN 1 ELSE 0 END) AS settles
                FROM paper_trades
                WHERE account_id=? AND execution_mode='depth-sim'
                GROUP BY decision_strategy
                """,
                (account_id,),
            ).fetchall():
                item = summary.get(row["decision_strategy"])
                if item is not None:
                    for key in ("trades", "buys", "sells", "settles"):
                        item[key] = int(row[key] or 0)

            for row in connection.execute(
                """
                SELECT entry_strategy, COALESCE(sum(realized_pnl), 0) AS pnl
                FROM paper_trades
                WHERE account_id=? AND execution_mode='depth-sim'
                GROUP BY entry_strategy
                """,
                (account_id,),
            ).fetchall():
                item = summary.get(row["entry_strategy"])
                if item is not None:
                    item["realized_pnl"] = float(row["pnl"] or 0.0)

            positions = connection.execute(
                """
                SELECT p.entry_strategy, p.outcome, p.shares, p.avg_cost,
                       b.bid_a, b.bid_b
                FROM paper_positions p
                LEFT JOIN market_snapshots b ON b.book_id=(
                    SELECT b2.book_id FROM market_snapshots b2
                    WHERE b2.match_id=p.match_id
                    ORDER BY b2.source_at DESC, b2.book_id DESC LIMIT 1
                )
                WHERE p.account_id=? AND p.shares>0
                  AND p.execution_mode='depth-sim'
                """,
                (account_id,),
            ).fetchall()

        for row in positions:
            item = summary.get(row["entry_strategy"])
            if item is None:
                continue
            shares = float(row["shares"])
            cost = shares * float(row["avg_cost"])
            bid = row["bid_a"] if row["outcome"] == "A" else row["bid_b"]
            mark = shares * float(bid or 0.0)
            item["open_positions"] += 1
            item["open_cost"] += cost
            item["mark_value"] += mark

        for item in summary.values():
            item["unrealized_pnl"] = item["mark_value"] - item["open_cost"]
            item["total_pnl"] = item["realized_pnl"] + item["unrealized_pnl"]
        return [summary[strategy] for strategy in STRATEGIES]

    def execution_summary(self, account_name: str = "live-paper") -> Dict[str, Any]:
        """Fill quality, rejection mix, risk controls, and worker liveness."""
        summary: Dict[str, Any] = {
            "mode": "depth-sim",
            "orders": 0,
            "pending": 0,
            "filled_orders": 0,
            "partial_orders": 0,
            "rejected_orders": 0,
            "requested_shares": 0.0,
            "filled_shares": 0.0,
            "fill_rate": None,
            "fees": 0.0,
            "avg_slippage": None,
            "avg_latency_ms": None,
            "depth_books": 0,
            "rejections": [],
            "kill_switch": False,
            "kill_switch_reason": "",
            "worker": self.executor_status(),
        }
        with self.connect() as connection:
            account = connection.execute(
                "SELECT account_id FROM paper_accounts WHERE name=?", (account_name,)
            ).fetchone()
            summary["depth_books"] = int(
                connection.execute(
                    "SELECT count(*) FROM market_snapshots WHERE depth_available=1"
                ).fetchone()[0]
            )
            if account is None:
                return summary
            account_id = int(account["account_id"])
            row = connection.execute(
                """
                SELECT count(*) AS orders,
                       sum(CASE WHEN status IN ('PENDING','SUBMITTED') THEN 1 ELSE 0 END) pending,
                       sum(CASE WHEN status='FILLED' THEN 1 ELSE 0 END) filled_orders,
                       sum(CASE WHEN status='PARTIALLY_FILLED' THEN 1 ELSE 0 END) partial_orders,
                       sum(CASE WHEN status IN ('REJECTED','EXPIRED','CANCELLED') THEN 1 ELSE 0 END) rejected_orders,
                       COALESCE(sum(requested_shares), 0) requested_shares,
                       COALESCE(sum(filled_shares), 0) filled_shares,
                       COALESCE(sum(fee_paid), 0) fees
                FROM paper_orders WHERE account_id=?
                """,
                (account_id,),
            ).fetchone()
            for key in (
                "orders", "pending", "filled_orders", "partial_orders",
                "rejected_orders",
            ):
                summary[key] = int(row[key] or 0)
            for key in ("requested_shares", "filled_shares", "fees"):
                summary[key] = float(row[key] or 0.0)
            if summary["requested_shares"] > 0:
                summary["fill_rate"] = (
                    summary["filled_shares"] / summary["requested_shares"]
                )
            quality = connection.execute(
                """
                SELECT avg(slippage) AS slippage, avg(fill_latency_ms) AS latency
                FROM paper_trades
                WHERE account_id=? AND execution_mode='depth-sim'
                  AND action IN ('BUY','SELL')
                """,
                (account_id,),
            ).fetchone()
            if quality["slippage"] is not None:
                summary["avg_slippage"] = float(quality["slippage"])
            if quality["latency"] is not None:
                summary["avg_latency_ms"] = float(quality["latency"])
            summary["rejections"] = [
                dict(item)
                for item in connection.execute(
                    """
                    SELECT rejection_reason AS reason, count(*) AS count
                    FROM paper_orders
                    WHERE account_id=? AND rejection_reason<>''
                    GROUP BY rejection_reason ORDER BY count(*) DESC, rejection_reason
                    LIMIT 8
                    """,
                    (account_id,),
                ).fetchall()
            ]
            control = connection.execute(
                "SELECT kill_switch, reason FROM execution_controls WHERE account_id=?",
                (account_id,),
            ).fetchone()
            if control is not None:
                summary["kill_switch"] = bool(control["kill_switch"])
                summary["kill_switch_reason"] = str(control["reason"] or "")
        return summary

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
                "grounded_priors": connection.execute(
                    "SELECT count(*) FROM llm_priors WHERE methodology='sound' "
                    "AND grounded_teams > 0"
                ).fetchone()[0],
                "excluded_priors": connection.execute(
                    "SELECT count(*) FROM llm_priors WHERE methodology != 'sound'"
                ).fetchone()[0],
                "states": connection.execute("SELECT count(*) FROM state_snapshots").fetchone()[0],
                "books": connection.execute("SELECT count(*) FROM market_snapshots").fetchone()[0],
                "forecasts": connection.execute("SELECT count(*) FROM forecasts").fetchone()[0],
                "priors": connection.execute("SELECT count(*) FROM llm_priors").fetchone()[0],
                "trades": connection.execute("SELECT count(*) FROM paper_trades").fetchone()[0],
                "orders": connection.execute("SELECT count(*) FROM paper_orders").fetchone()[0],
                "fills": connection.execute("SELECT count(*) FROM paper_fills").fetchone()[0],
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
            "state_guard": self.state_rejection_summary(),
            "scoring": self.scoring_summary(),
            "shadow": self.shadow_scoring_summary(),
            "matches": self.latest_rows(),
            "account": self.account_payload(account_name),
            "execution": self.execution_summary(account_name),
        }

    def shadow_scoring_summary(self) -> Dict[str, Any]:
        """Shadow-panel scoreboard. Imported lazily to keep storage leaf-level."""
        from .scoring import shadow_score

        return shadow_score(self)

    def scoring_summary(self) -> Dict[str, Any]:
        """Scoreboard for the dashboard. Imported lazily to keep storage leaf-level."""
        from .scoring import score

        report = score(self)
        report["matches"] = report["matches"][:20]
        with self.connect() as connection:
            report["excluded"] = connection.execute(
                "SELECT count(*) FROM llm_priors WHERE methodology != 'sound'"
            ).fetchone()[0]
        return report
