"""Depth-aware deterministic paper execution.

Strategies create IOC orders; this module is the only path that can create
fills, move cash, or update a position.  Live mode fetches a fresh CLOB depth
snapshot after the configured latency.  Replay mode supplies the recorded
snapshot directly and uses the same matching and risk code.
"""

import json
import os
import socket
import time
from dataclasses import dataclass, field, fields
from datetime import timedelta, timezone
from typing import Any, Dict, List, Optional

from .paper import PaperConfig
from .polymarket import PolymarketBookClient
from .storage import Database
from .timeutil import canonical_timestamp, isoformat, parse_timestamp, utc_now
from .types import BookQuote


EPSILON = 1e-9


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    """Current Polymarket taker curve, rounded to venue USDC precision."""
    return round(
        max(0.0, float(shares))
        * max(0.0, float(fee_rate))
        * float(price)
        * (1.0 - float(price)),
        5,
    )


def _config(payload: Any) -> PaperConfig:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    source = payload if isinstance(payload, dict) else {}
    accepted = {item.name for item in fields(PaperConfig)}
    settings = PaperConfig(**{key: value for key, value in source.items() if key in accepted})
    settings.validate()
    return settings


@dataclass
class ExecutionBatch:
    processed: int = 0
    filled: int = 0
    partial: int = 0
    rejected: int = 0
    retried: int = 0
    errors: List[str] = field(default_factory=list)
    orders: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "processed": self.processed,
            "filled": self.filled,
            "partial": self.partial,
            "rejected": self.rejected,
            "retried": self.retried,
            "errors": list(self.errors),
            "orders": list(self.orders),
        }


def _day_start(timestamp: str) -> str:
    value = parse_timestamp(timestamp).astimezone(timezone.utc)
    return isoformat(value.replace(hour=0, minute=0, second=0, microsecond=0))


def _reject(
    database: Database, order_id: int, reason: str, completed_at: str
) -> Dict[str, Any]:
    database.reject_order(order_id, reason, completed_at)
    return {"order_id": int(order_id), "status": "REJECTED", "reason": reason}


def execute_claimed_order(
    database: Database,
    order: Dict[str, Any],
    quote: BookQuote,
    completed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Match one claimed IOC order against a specific immutable book."""
    completed = canonical_timestamp(completed_at or isoformat(utc_now()))
    settings = _config(order.get("config_json"))
    order_id = int(order["order_id"])
    action = str(order["action"])
    outcome = str(order["outcome"])

    if quote.match_id != order["match_id"]:
        return _reject(database, order_id, "book_match_mismatch", completed)
    if action == "BUY":
        if not bool(order.get("forecast_entry_enabled")):
            return _reject(database, order_id, "entry_disabled", completed)
        if order.get("match_status") != "open" or bool(order.get("match_ended")):
            return _reject(database, order_id, "match_not_open", completed)
        control = database.execution_kill_switch(int(order["account_id"]))
        if bool(control.get("kill_switch")):
            return _reject(database, order_id, "kill_switch", completed)

    normalized = quote.normalized()
    book_age = (
        parse_timestamp(completed) - parse_timestamp(normalized.observed_at)
    ).total_seconds()
    if book_age < -5.0 or book_age > float(settings.max_book_age_seconds):
        return _reject(database, order_id, "stale_book", completed)

    side = "asks" if action == "BUY" else "bids"
    levels = normalized.levels(outcome, side)
    if not levels:
        return _reject(database, order_id, "missing_depth", completed)
    book_id = database.record_book(normalized, store_depth=True)

    with database.connect() as connection:
        current = connection.execute(
            "SELECT * FROM paper_orders WHERE order_id=?", (order_id,)
        ).fetchone()
        if current is None or current["status"] != "SUBMITTED":
            return {
                "order_id": order_id,
                "status": "IGNORED",
                "reason": "order_not_submitted",
            }
        account = connection.execute(
            "SELECT * FROM paper_accounts WHERE account_id=?",
            (int(order["account_id"]),),
        ).fetchone()
        if account is None:
            raise ValueError("paper account disappeared during execution")

        connection.execute(
            """
            INSERT OR IGNORE INTO paper_positions(
                account_id, match_id, outcome, shares, avg_cost, realized_pnl,
                entry_strategy, execution_mode, fees_paid, updated_at
            ) VALUES(?, ?, ?, 0, 0, 0, ?, 'depth-sim', 0, ?)
            """,
            (
                int(order["account_id"]), order["match_id"], outcome,
                order["entry_strategy"], completed,
            ),
        )
        position = connection.execute(
            """
            SELECT * FROM paper_positions
            WHERE account_id=? AND match_id=? AND outcome=?
            """,
            (int(order["account_id"]), order["match_id"], outcome),
        ).fetchone()

        requested = float(order["requested_shares"])
        max_shares = requested
        budget = float("inf")
        risk_reason = ""

        if action == "BUY":
            daily = connection.execute(
                """
                SELECT COALESCE(sum(realized_pnl), 0) AS pnl
                FROM paper_trades
                WHERE account_id=? AND execution_mode='depth-sim'
                  AND traded_at >= ?
                """,
                (int(order["account_id"]), _day_start(completed)),
            ).fetchone()
            if float(daily["pnl"] or 0.0) <= -(
                float(account["initial_cash"]) * settings.daily_loss_limit_fraction
            ):
                risk_reason = "daily_loss_limit"

            open_positions = connection.execute(
                """
                SELECT count(*) AS n FROM paper_positions
                WHERE account_id=? AND shares>?
                """,
                (int(order["account_id"]), EPSILON),
            ).fetchone()
            if (
                not risk_reason
                and float(position["shares"]) <= EPSILON
                and int(open_positions["n"]) >= int(settings.max_open_positions)
            ):
                risk_reason = "max_open_positions"

            match_row = connection.execute(
                """
                SELECT COALESCE(sum(shares*avg_cost), 0) AS open_cost,
                       COALESCE(sum(realized_pnl), 0) AS realized
                FROM paper_positions WHERE account_id=? AND match_id=?
                """,
                (int(order["account_id"]), order["match_id"]),
            ).fetchone()
            portfolio = connection.execute(
                """
                SELECT COALESCE(sum(shares*avg_cost), 0) AS open_cost
                FROM paper_positions WHERE account_id=?
                """,
                (int(order["account_id"]),),
            ).fetchone()
            match_cap = (
                float(account["initial_cash"]) * settings.max_match_fraction
                + float(match_row["realized"] or 0.0)
                - float(match_row["open_cost"] or 0.0)
            )
            portfolio_cap = (
                float(account["initial_cash"])
                * settings.max_total_exposure_fraction
                - float(portfolio["open_cost"] or 0.0)
            )
            budget = min(float(account["cash"]), match_cap, portfolio_cap)
            if not risk_reason and budget <= EPSILON:
                risk_reason = "risk_budget_exhausted"
        else:
            max_shares = min(requested, float(position["shares"] or 0.0))
            if max_shares <= EPSILON:
                risk_reason = "no_position_to_sell"

        if risk_reason:
            connection.execute(
                """
                UPDATE paper_orders
                SET status='REJECTED', rejection_reason=?, execution_book_id=?,
                    completed_at=?, updated_at=?
                WHERE order_id=?
                """,
                (risk_reason, book_id, completed, completed, order_id),
            )
            return {"order_id": order_id, "status": "REJECTED", "reason": risk_reason}

        remaining = max_shares
        fills: List[Dict[str, float]] = []
        limit_price = float(order["limit_price"])
        partial_reason = ""
        for level_index, level in enumerate(levels):
            if remaining <= EPSILON:
                break
            if action == "BUY" and level.price > limit_price + EPSILON:
                partial_reason = "limit_price_reached"
                break
            if action == "SELL" and level.price < limit_price - EPSILON:
                partial_reason = "limit_price_reached"
                break
            available = level.size * float(settings.max_market_participation)
            shares = min(remaining, available)
            if action == "BUY":
                unit_fee = settings.taker_fee_rate * level.price * (1.0 - level.price)
                shares = min(shares, max(0.0, budget) / (level.price + unit_fee))
            if shares <= EPSILON:
                continue
            notional = shares * level.price
            fee = taker_fee(shares, level.price, settings.taker_fee_rate)
            if action == "BUY" and notional + fee > budget + EPSILON:
                shares *= max(0.0, budget) / max(EPSILON, notional + fee)
                notional = shares * level.price
                fee = taker_fee(shares, level.price, settings.taker_fee_rate)
            if shares <= EPSILON:
                continue
            fills.append(
                {
                    "level_index": float(level_index),
                    "shares": shares,
                    "price": level.price,
                    "notional": notional,
                    "fee": fee,
                }
            )
            remaining -= shares
            if action == "BUY":
                budget -= notional + fee
                if budget <= EPSILON and remaining > EPSILON:
                    partial_reason = "risk_budget_partial"
                    break

        filled_shares = sum(item["shares"] for item in fills)
        if filled_shares <= EPSILON:
            reason = "limit_not_marketable" if levels else "missing_depth"
            connection.execute(
                """
                UPDATE paper_orders
                SET status='REJECTED', rejection_reason=?, execution_book_id=?,
                    completed_at=?, updated_at=?
                WHERE order_id=?
                """,
                (reason, book_id, completed, completed, order_id),
            )
            return {"order_id": order_id, "status": "REJECTED", "reason": reason}

        total_notional = sum(item["notional"] for item in fills)
        total_fee = sum(item["fee"] for item in fills)
        average = total_notional / filled_shares
        for item in fills:
            connection.execute(
                """
                INSERT INTO paper_fills(
                    order_id, book_id, level_index, action, outcome, shares,
                    price, notional, fee, filled_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id, book_id, int(item["level_index"]), action, outcome,
                    item["shares"], item["price"], item["notional"],
                    item["fee"], completed,
                ),
            )

        old_shares = float(position["shares"] or 0.0)
        old_avg = float(position["avg_cost"] or 0.0)
        old_realized = float(position["realized_pnl"] or 0.0)
        old_fees = float(position["fees_paid"] or 0.0)
        entry_strategy = str(position["entry_strategy"] or order["entry_strategy"])
        if action == "BUY":
            total_cost = total_notional + total_fee
            new_shares = old_shares + filled_shares
            new_avg = (old_shares * old_avg + total_cost) / new_shares
            cash_delta = -total_cost
            realized = 0.0
            if old_shares <= EPSILON:
                entry_strategy = str(order["entry_strategy"])
            connection.execute(
                """
                UPDATE paper_positions
                SET shares=?, avg_cost=?, entry_strategy=?, execution_mode='depth-sim',
                    fees_paid=?, updated_at=?
                WHERE account_id=? AND match_id=? AND outcome=?
                """,
                (
                    new_shares, new_avg, entry_strategy, old_fees + total_fee,
                    completed, int(order["account_id"]), order["match_id"], outcome,
                ),
            )
        else:
            net_proceeds = total_notional - total_fee
            realized = net_proceeds - filled_shares * old_avg
            new_shares = max(0.0, old_shares - filled_shares)
            new_avg = old_avg if new_shares > EPSILON else 0.0
            cash_delta = net_proceeds
            connection.execute(
                """
                UPDATE paper_positions
                SET shares=?, avg_cost=?, realized_pnl=?, fees_paid=?, updated_at=?
                WHERE account_id=? AND match_id=? AND outcome=?
                """,
                (
                    new_shares, new_avg, old_realized + realized,
                    old_fees + total_fee, completed, int(order["account_id"]),
                    order["match_id"], outcome,
                ),
            )

        connection.execute(
            "UPDATE paper_accounts SET cash=cash+?, updated_at=? WHERE account_id=?",
            (cash_delta, completed, int(order["account_id"])),
        )
        partial = filled_shares + EPSILON < requested
        status = "PARTIALLY_FILLED" if partial else "FILLED"
        rejection_reason = (
            partial_reason or "insufficient_executable_depth" if partial else ""
        )
        latency_ms = max(
            0.0,
            (parse_timestamp(completed) - parse_timestamp(order["signal_at"])).total_seconds()
            * 1000.0,
        )
        slippage = (
            average - float(order["signal_price"])
            if action == "BUY"
            else float(order["signal_price"]) - average
        )
        connection.execute(
            """
            UPDATE paper_orders
            SET status=?, filled_shares=?, avg_fill_price=?, fee_paid=?,
                cash_delta=?, realized_pnl=?, execution_book_id=?,
                rejection_reason=?, completed_at=?, updated_at=?
            WHERE order_id=?
            """,
            (
                status, filled_shares, average, total_fee, cash_delta, realized,
                book_id, rejection_reason, completed, completed, order_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO paper_trades(
                account_id, match_id, forecast_id, action, outcome, shares,
                price, cash_delta, realized_pnl, decision_strategy,
                entry_strategy, reason, traded_at, execution_mode, order_id,
                fee, slippage, signal_price, fill_latency_ms
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'depth-sim', ?, ?, ?, ?, ?)
            """,
            (
                int(order["account_id"]), order["match_id"], int(order["forecast_id"]),
                action, outcome, filled_shares, average, cash_delta, realized,
                order["decision_strategy"], entry_strategy, order["reason"],
                completed, order_id, total_fee, slippage,
                float(order["signal_price"]), latency_ms,
            ),
        )

    return {
        "order_id": order_id,
        "status": status,
        "action": action,
        "outcome": outcome,
        "requested_shares": requested,
        "filled_shares": filled_shares,
        "fill_rate": filled_shares / requested,
        "avg_fill_price": average,
        "signal_price": float(order["signal_price"]),
        "slippage": slippage,
        "fee": total_fee,
        "cash_delta": cash_delta,
        "realized_pnl": realized,
        "book_id": book_id,
        "latency_ms": latency_ms,
        "levels": len(fills),
        "reason": rejection_reason,
    }


def process_due_orders(
    database: Database,
    account_name: str,
    book_client: Optional[PolymarketBookClient] = None,
    now: Optional[str] = None,
    limit: int = 50,
    supplied_quotes: Optional[Dict[str, BookQuote]] = None,
) -> ExecutionBatch:
    database.initialize()
    client = book_client or PolymarketBookClient()
    reference = canonical_timestamp(now or isoformat(utc_now()))
    result = ExecutionBatch()
    for order_id in database.due_order_ids(account_name, reference, limit=limit):
        submitted = reference if now is not None else isoformat(utc_now())
        order = database.claim_order(order_id, submitted)
        if order is None:
            continue
        result.processed += 1
        try:
            quote = (supplied_quotes or {}).get(order["match_id"])
            if quote is None:
                if not order.get("token_a") or not order.get("token_b"):
                    outcome = _reject(database, order_id, "missing_token_ids", submitted)
                    result.rejected += 1
                    result.orders.append(outcome)
                    continue
                quote = client.get_pair(
                    order["match_id"], order["token_a"], order["token_b"]
                )
            completed = reference if now is not None else isoformat(utc_now())
            outcome = execute_claimed_order(database, order, quote, completed_at=completed)
            result.orders.append(outcome)
            if outcome["status"] == "FILLED":
                result.filled += 1
            elif outcome["status"] == "PARTIALLY_FILLED":
                result.partial += 1
            elif outcome["status"] == "REJECTED":
                result.rejected += 1
        except Exception as error:
            settings = _config(order.get("config_json"))
            message = "%s: %s" % (type(error).__name__, error)
            if (
                int(order.get("attempts") or 0) < int(settings.max_attempts)
                and parse_timestamp(submitted) < parse_timestamp(order["expires_at"])
            ):
                retry_at = parse_timestamp(submitted) + timedelta(seconds=1)
                database.retry_order(order_id, isoformat(retry_at), message)
                result.retried += 1
            else:
                database.reject_order(order_id, "market_data_unavailable", submitted)
                result.rejected += 1
            result.errors.append("order %d: %s" % (order_id, message))
    return result


def run_executor_loop(
    database: Database,
    account_name: str,
    interval_seconds: float = 0.25,
    cycles: int = 0,
    book_client: Optional[PolymarketBookClient] = None,
) -> None:
    """Continuously execute due orders; ``cycles=0`` means forever."""
    if interval_seconds <= 0.0:
        raise ValueError("executor interval_seconds must be positive")
    worker_id = "%s:%s:%d" % (socket.gethostname(), os.getpid(), int(time.time()))
    database.initialize()
    database.ensure_account(account_name)
    database.update_executor_status(worker_id=worker_id, status="running")
    count = 0
    last_heartbeat = time.monotonic()
    while cycles <= 0 or count < cycles:
        count += 1
        try:
            batch = process_due_orders(
                database, account_name, book_client=book_client, limit=50
            )
            heartbeat_due = time.monotonic() - last_heartbeat >= 5.0
            if batch.processed or batch.errors or heartbeat_due:
                database.update_executor_status(
                    worker_id=worker_id,
                    status="running",
                    processed_delta=batch.processed,
                    filled_delta=batch.filled,
                    partial_delta=batch.partial,
                    rejected_delta=batch.rejected,
                    errors_delta=len(batch.errors),
                    last_error=batch.errors[-1] if batch.errors else "",
                    touched_order=batch.processed > 0,
                )
                last_heartbeat = time.monotonic()
            if batch.processed or batch.errors:
                print(json.dumps(batch.to_dict(), ensure_ascii=False), flush=True)
        except Exception as error:
            database.update_executor_status(
                worker_id=worker_id,
                status="degraded",
                errors_delta=1,
                last_error="%s: %s" % (type(error).__name__, error),
            )
        if cycles <= 0 or count < cycles:
            time.sleep(float(interval_seconds))
