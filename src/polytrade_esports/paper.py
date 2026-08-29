import hashlib
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Dict, List, Optional

from .storage import Database
from .state_guard import validate_strategy
from .timeutil import canonical_timestamp, isoformat, parse_timestamp, utc_now
from .types import BookQuote


@dataclass(frozen=True)
class PaperConfig:
    min_entry_edge: float = 0.10
    exit_edge: float = 0.00
    # How far the market may travel, while our own state feed says nothing new,
    # before an apparent edge is treated as ignorance rather than opportunity.
    max_market_drift: float = 0.08
    max_match_fraction: float = 0.01
    max_total_exposure_fraction: float = 0.05
    max_open_positions: int = 8
    daily_loss_limit_fraction: float = 0.03
    kelly_scale: float = 0.25
    # Ordinary target adjustments below either floor are ignored. Forced
    # exits (edge gone or side flip) deliberately bypass these churn filters.
    min_order_notional: float = 0.50
    rebalance_tolerance_fraction: float = 0.05
    latency_ms: int = 1250
    latency_jitter_ms: int = 250
    order_ttl_seconds: float = 8.0
    max_book_age_seconds: float = 5.0
    max_slippage: float = 0.03
    max_market_participation: float = 0.10
    taker_fee_rate: float = 0.03
    max_attempts: int = 3

    def validate(self) -> None:
        if not 0.0 <= self.exit_edge <= self.min_entry_edge < 1.0:
            raise ValueError("require 0 <= exit_edge <= min_entry_edge < 1")
        if not 0.0 < self.max_market_drift <= 1.0:
            raise ValueError("max_market_drift must be in (0, 1]")
        if not 0.0 < self.max_match_fraction <= 0.10:
            raise ValueError("max_match_fraction must be in (0, 0.10]")
        if not self.max_match_fraction <= self.max_total_exposure_fraction <= 1.0:
            raise ValueError(
                "max_total_exposure_fraction must be >= max_match_fraction and <= 1"
            )
        if int(self.max_open_positions) <= 0:
            raise ValueError("max_open_positions must be positive")
        if not 0.0 < self.daily_loss_limit_fraction <= 1.0:
            raise ValueError("daily_loss_limit_fraction must be in (0, 1]")
        if not 0.0 < self.kelly_scale <= 1.0:
            raise ValueError("kelly_scale must be in (0, 1]")
        if float(self.min_order_notional) < 0.0:
            raise ValueError("min_order_notional cannot be negative")
        if not 0.0 <= float(self.rebalance_tolerance_fraction) <= 1.0:
            raise ValueError("rebalance_tolerance_fraction must be in [0, 1]")
        if int(self.latency_ms) < 0 or int(self.latency_jitter_ms) < 0:
            raise ValueError("execution latency cannot be negative")
        if int(self.latency_jitter_ms) > int(self.latency_ms):
            raise ValueError("latency_jitter_ms cannot exceed latency_ms")
        if float(self.order_ttl_seconds) <= 0.0:
            raise ValueError("order_ttl_seconds must be positive")
        if float(self.max_book_age_seconds) <= 0.0:
            raise ValueError("max_book_age_seconds must be positive")
        if not 0.0 <= self.max_slippage < 1.0:
            raise ValueError("max_slippage must be in [0, 1)")
        if not 0.0 < self.max_market_participation <= 1.0:
            raise ValueError("max_market_participation must be in (0, 1]")
        if not 0.0 <= self.taker_fee_rate <= 1.0:
            raise ValueError("taker_fee_rate must be in [0, 1]")
        if int(self.max_attempts) <= 0:
            raise ValueError("max_attempts must be positive")


def _kelly_cost(
    bankroll: float,
    probability: float,
    price: float,
    config: PaperConfig,
) -> float:
    edge = probability - price
    if edge <= 0:
        return 0.0
    full_kelly = edge / max(1e-12, 1.0 - price)
    fraction = min(config.max_match_fraction, config.kelly_scale * full_kelly)
    return bankroll * max(0.0, fraction)


def rebalance(
    database: Database,
    account_name: str,
    forecast_id: int,
    match_id: str,
    probability_a: float,
    quote: BookQuote,
    config: Optional[PaperConfig] = None,
    market_drift: Optional[float] = None,
    entry_enabled: bool = True,
    strategy: str = "pre-match",
    signal_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Plan IOC paper orders; never mutate cash or positions directly.

    The execution worker is the only component allowed to turn an order into
    fills and update the portfolio.  Keeping the strategy boundary here makes
    delayed/partial/rejected orders visible instead of silently pretending the
    target position already exists.
    """
    settings = config or PaperConfig()
    settings.validate()
    strategy = validate_strategy(strategy)
    account_id = database.ensure_account(account_name)
    q = quote.normalized()
    now = canonical_timestamp(signal_at or isoformat(utc_now()))
    probability = {"A": probability_a, "B": 1.0 - probability_a}
    ask = {"A": q.ask_a, "B": q.ask_b}
    bid = {"A": q.bid_a, "B": q.bid_b}
    edge = {
        "A": probability["A"] - ask["A"],
        "B": probability["B"] - ask["B"],
    }
    candidate = "A" if edge["A"] >= edge["B"] else "B"
    entry_side = candidate if edge[candidate] + 1e-12 >= settings.min_entry_edge else None

    # A stale or disconnected live-state feed may still be used to mark books
    # and reduce existing risk, but never to open or increase a position.
    if not entry_enabled:
        entry_side = None

    # The model only moves when the state feed moves. Between state changes it
    # is a constant, so any edge that opened up in that window is entirely the
    # market moving -- which means the market has learned something the feed
    # has not shown us, and the "edge" is our blindness, not their error.
    #
    # Observed cost of not having this guard: the model sat at 0.210 for five
    # hours at 0-0 while the market ran from 0.205 to 0.425 and back to 0.085.
    # The 0.085 was map one ending. We read it as a 12-point edge, bought, and
    # sold flat a minute later once our own feed caught up.
    #
    # Exits are deliberately left alone: being blind is a reason to stop
    # opening positions, never a reason to keep one you would otherwise close.
    if entry_side is not None and market_drift is not None:
        if abs(float(market_drift)) > settings.max_market_drift:
            entry_side = None

    # A forecast supersedes only orders that have not reached the simulated
    # venue. A submitted order is owned by the executor and remains live. Do
    # this before measuring committed exposure so superseded PENDING orders do
    # not reserve risk budget forever.
    database.cancel_pending_orders(
        account_id, match_id, except_forecast_id=forecast_id
    )

    with database.connect() as connection:
        account = connection.execute(
            "SELECT * FROM paper_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        rows = connection.execute(
            """
            SELECT * FROM paper_positions
            WHERE account_id=? AND match_id=?
            """,
            (account_id, match_id),
        ).fetchall()
        portfolio = connection.execute(
            """
            SELECT COALESCE(sum(shares*avg_cost), 0) AS open_cost
            FROM paper_positions WHERE account_id=?
            """,
            (account_id,),
        ).fetchone()
        committed_rows = connection.execute(
            """
            SELECT match_id, outcome, requested_shares, limit_price
            FROM paper_orders
            WHERE account_id=? AND action='BUY'
              AND status IN ('PENDING', 'SUBMITTED')
              AND forecast_id<>?
            """,
            (account_id, forecast_id),
        ).fetchall()
    positions: Dict[str, Dict[str, Any]] = {
        "A": {"shares": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0,
              "entry_strategy": strategy, "execution_mode": "depth-sim"},
        "B": {"shares": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0,
              "entry_strategy": strategy, "execution_mode": "depth-sim"},
    }
    for row in rows:
        positions[str(row["outcome"])] = dict(row)

    committed_shares = {"A": 0.0, "B": 0.0}
    committed_match_cost = 0.0
    committed_portfolio_cost = 0.0
    for row in committed_rows:
        committed_price = float(row["limit_price"])
        committed_unit_fee = (
            settings.taker_fee_rate
            * committed_price
            * (1.0 - committed_price)
        )
        committed_cost = float(row["requested_shares"]) * (
            committed_price + committed_unit_fee
        )
        committed_portfolio_cost += committed_cost
        if str(row["match_id"]) == match_id:
            committed_match_cost += committed_cost
            outcome = str(row["outcome"])
            if outcome in committed_shares:
                committed_shares[outcome] += float(row["requested_shares"])

    match_open_cost = sum(
        float(positions[outcome].get("shares") or 0.0)
        * float(positions[outcome].get("avg_cost") or 0.0)
        for outcome in ("A", "B")
    )
    realized = sum(
        float(positions[outcome].get("realized_pnl") or 0.0)
        for outcome in ("A", "B")
    )
    max_loss = float(account["initial_cash"]) * settings.max_match_fraction
    available_buy_budget = min(
        float(account["cash"]) - committed_portfolio_cost,
        max_loss + realized - match_open_cost - committed_match_cost,
        (
            float(account["initial_cash"])
            * settings.max_total_exposure_fraction
            - float(portfolio["open_cost"] or 0.0)
            - committed_portfolio_cost
        ),
    )

    planned: List[Dict[str, Any]] = []

    def add_order(outcome: str, action: str, shares: float, reason: str) -> None:
        amount = float(shares)
        if amount <= 1e-9:
            return
        price = ask[outcome] if action == "BUY" else bid[outcome]
        limit_price = (
            min(0.999999, price + settings.max_slippage)
            if action == "BUY"
            else max(0.000001, price - settings.max_slippage)
        )
        seed = "%s:%s:%s:%s:%s" % (
            account_id, forecast_id, action, outcome, reason
        )
        client_order_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        jitter_span = int(settings.latency_jitter_ms) * 2 + 1
        jitter = (
            int(client_order_id[:8], 16) % jitter_span
            - int(settings.latency_jitter_ms)
            if jitter_span > 1
            else 0
        )
        latency_ms = int(settings.latency_ms) + jitter
        due_dt = parse_timestamp(now) + timedelta(milliseconds=latency_ms)
        expiry_dt = due_dt + timedelta(seconds=float(settings.order_ttl_seconds))
        position = positions[outcome]
        entry_strategy = (
            str(position.get("entry_strategy") or strategy)
            if float(position.get("shares") or 0.0) > 1e-9
            else strategy
        )
        config_payload = asdict(settings)
        config_payload["effective_latency_ms"] = latency_ms
        order = database.create_paper_order(
            client_order_id=client_order_id,
            account_id=account_id,
            match_id=match_id,
            forecast_id=forecast_id,
            decision_strategy=strategy,
            entry_strategy=entry_strategy,
            action=action,
            outcome=outcome,
            requested_shares=amount,
            signal_price=price,
            limit_price=limit_price,
            signal_at=now,
            execute_after=isoformat(due_dt),
            expires_at=isoformat(expiry_dt),
            reason=reason,
            config=config_payload,
        )
        planned.append(
            {
                "order_id": int(order["order_id"]),
                "status": order["status"],
                "action": action,
                "outcome": outcome,
                "shares": amount,
                "signal_price": price,
                "limit_price": limit_price,
                "execute_after": order["execute_after"],
                "reason": reason,
                "decision_strategy": strategy,
                "entry_strategy": entry_strategy,
            }
        )

    # Close or flip risk first; order ids preserve this execution sequence.
    for outcome in ("A", "B"):
        current_shares = float(positions[outcome].get("shares") or 0.0)
        if current_shares <= 1e-9:
            continue
        if entry_side is not None and entry_side != outcome:
            add_order(outcome, "SELL", current_shares, "side_flip")
        elif probability[outcome] - bid[outcome] <= settings.exit_edge:
            add_order(outcome, "SELL", current_shares, "edge_gone")

    if entry_side is not None:
        target_cost = _kelly_cost(
            float(account["initial_cash"]),
            probability[entry_side],
            ask[entry_side],
            settings,
        )
        target_cost = min(target_cost, max(0.0, max_loss + realized))
        # Size against the all-in signal cost. Otherwise an order placed
        # exactly at the match-risk ceiling is guaranteed to be reported as a
        # tiny partial fill once the executor applies taker fees.
        signal_unit_fee = (
            settings.taker_fee_rate
            * ask[entry_side]
            * (1.0 - ask[entry_side])
        )
        # Venue fees are rounded to five decimals. Reserve one precision unit
        # so rounding upward cannot turn an otherwise exact target into a
        # microscopic PARTIALLY_FILLED order.
        target_shares = max(0.0, target_cost - 0.00001) / (
            ask[entry_side] + signal_unit_fee
        )
        current_shares = float(positions[entry_side].get("shares") or 0.0)
        effective_shares = current_shares + committed_shares[entry_side]
        adjustment_floor = max(
            float(settings.min_order_notional),
            target_cost * float(settings.rebalance_tolerance_fraction),
        )
        if target_shares > effective_shares + 1e-9:
            desired_shares = target_shares - effective_shares
            desired_cost = desired_shares * (
                ask[entry_side] + signal_unit_fee
            )
            # This is an advisory sizing/preflight check. The executor still
            # re-reads cash, positions and both caps atomically at fill time.
            order_cost = min(desired_cost, max(0.0, available_buy_budget))
            if order_cost + 1e-9 >= adjustment_floor:
                add_order(
                    entry_side,
                    "BUY",
                    order_cost / (ask[entry_side] + signal_unit_fee),
                    "entry_or_increase",
                )
        elif current_shares > target_shares + 1e-9:
            reduction_shares = current_shares - target_shares
            reduction_notional = reduction_shares * bid[entry_side]
            if reduction_notional + 1e-9 >= adjustment_floor:
                add_order(
                    entry_side, "SELL", reduction_shares,
                    "target_reduction",
                )

    return planned


def settle_match(
    database: Database,
    account_name: str,
    match_id: str,
    winner: str,
    forecast_id: int,
) -> List[Dict[str, Any]]:
    if winner not in ("A", "B"):
        raise ValueError("winner must be A or B")
    account_id = database.ensure_account(account_name)
    now = isoformat(utc_now())
    actions: List[Dict[str, Any]] = []
    with database.connect() as connection:
        connection.execute(
            """
            UPDATE paper_orders
            SET status='CANCELLED', rejection_reason='match_resolved',
                completed_at=?, updated_at=?
            WHERE account_id=(SELECT account_id FROM paper_accounts WHERE name=?)
              AND match_id=? AND status IN ('PENDING', 'SUBMITTED')
            """,
            (now, now, account_name, match_id),
        )
        account = connection.execute(
            "SELECT * FROM paper_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        cash = float(account["cash"])
        positions = connection.execute(
            """
            SELECT * FROM paper_positions
            WHERE account_id=? AND match_id=? AND shares>0
            """,
            (account_id, match_id),
        ).fetchall()
        for position in positions:
            shares = float(position["shares"])
            payout_price = 1.0 if position["outcome"] == winner else 0.0
            payout = shares * payout_price
            pnl = payout - (shares * float(position["avg_cost"]))
            entry_strategy = str(position["entry_strategy"] or "pre-match")
            execution_mode = str(position["execution_mode"] or "legacy")
            cash += payout
            connection.execute(
                """
                UPDATE paper_positions
                SET shares=0, avg_cost=0, realized_pnl=realized_pnl+?, updated_at=?
                WHERE account_id=? AND match_id=? AND outcome=?
                """,
                (pnl, now, account_id, match_id, position["outcome"]),
            )
            connection.execute(
                """
                INSERT INTO paper_trades(
                    account_id, match_id, forecast_id, action, outcome, shares,
                    price, cash_delta, realized_pnl, decision_strategy,
                    entry_strategy, reason, traded_at, execution_mode
                ) VALUES(?, ?, ?, 'SETTLE', ?, ?, ?, ?, ?, ?, ?, 'resolution', ?, ?)
                """,
                (
                    account_id,
                    match_id,
                    forecast_id,
                    position["outcome"],
                    shares,
                    payout_price,
                    payout,
                    pnl,
                    entry_strategy,
                    entry_strategy,
                    now,
                    execution_mode,
                ),
            )
            actions.append(
                {"action": "SETTLE", "outcome": position["outcome"], "pnl": pnl}
            )
        connection.execute(
            "UPDATE paper_accounts SET cash=?, updated_at=? WHERE account_id=?",
            (cash, now, account_id),
        )
    return actions
