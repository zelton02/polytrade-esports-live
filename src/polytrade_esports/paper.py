from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .storage import Database
from .timeutil import isoformat, utc_now
from .types import BookQuote


@dataclass(frozen=True)
class PaperConfig:
    min_entry_edge: float = 0.10
    exit_edge: float = 0.00
    max_match_fraction: float = 0.01
    kelly_scale: float = 0.25

    def validate(self) -> None:
        if not 0.0 <= self.exit_edge <= self.min_entry_edge < 1.0:
            raise ValueError("require 0 <= exit_edge <= min_entry_edge < 1")
        if not 0.0 < self.max_match_fraction <= 0.10:
            raise ValueError("max_match_fraction must be in (0, 0.10]")
        if not 0.0 < self.kelly_scale <= 1.0:
            raise ValueError("kelly_scale must be in (0, 1]")


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
) -> List[Dict[str, Any]]:
    settings = config or PaperConfig()
    settings.validate()
    account_id = database.ensure_account(account_name)
    q = quote.normalized()
    now = isoformat(utc_now())
    probability = {"A": probability_a, "B": 1.0 - probability_a}
    ask = {"A": q.ask_a, "B": q.ask_b}
    bid = {"A": q.bid_a, "B": q.bid_b}
    edge = {
        "A": probability["A"] - ask["A"],
        "B": probability["B"] - ask["B"],
    }
    candidate = "A" if edge["A"] >= edge["B"] else "B"
    entry_side = candidate if edge[candidate] + 1e-12 >= settings.min_entry_edge else None
    actions: List[Dict[str, Any]] = []

    with database.connect() as connection:
        account = connection.execute(
            "SELECT * FROM paper_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        for outcome in ("A", "B"):
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_positions(
                    account_id, match_id, outcome, shares, avg_cost, realized_pnl, updated_at
                ) VALUES(?, ?, ?, 0, 0, 0, ?)
                """,
                (account_id, match_id, outcome, now),
            )

        positions = {
            row["outcome"]: row
            for row in connection.execute(
                """
                SELECT * FROM paper_positions
                WHERE account_id=? AND match_id=?
                """,
                (account_id, match_id),
            ).fetchall()
        }
        cash = float(account["cash"])

        def match_realized_pnl() -> float:
            return sum(float(positions[outcome]["realized_pnl"]) for outcome in ("A", "B"))

        def open_cost_basis() -> float:
            return sum(
                float(positions[outcome]["shares"]) * float(positions[outcome]["avg_cost"])
                for outcome in ("A", "B")
            )

        def remaining_risk_budget() -> float:
            # Total worst-case match loss stays within the original per-match cap.
            # A realized loss consumes the remaining budget instead of triggering
            # a fresh full-size bet on the other side.
            max_loss = float(account["initial_cash"]) * settings.max_match_fraction
            allowed_open_cost = max(0.0, max_loss + match_realized_pnl())
            return max(0.0, allowed_open_cost - open_cost_basis())

        def sell(outcome: str, shares: float, reason: str) -> None:
            nonlocal cash
            if shares <= 1e-12:
                return
            position = positions[outcome]
            available = float(position["shares"])
            amount = min(available, shares)
            price = bid[outcome]
            proceeds = amount * price
            cost_basis = amount * float(position["avg_cost"])
            pnl = proceeds - cost_basis
            remaining = available - amount
            new_avg = float(position["avg_cost"]) if remaining > 1e-12 else 0.0
            new_realized = float(position["realized_pnl"]) + pnl
            cash += proceeds
            connection.execute(
                """
                UPDATE paper_positions
                SET shares=?, avg_cost=?, realized_pnl=?, updated_at=?
                WHERE account_id=? AND match_id=? AND outcome=?
                """,
                (remaining, new_avg, new_realized, now, account_id, match_id, outcome),
            )
            connection.execute(
                """
                INSERT INTO paper_trades(
                    account_id, match_id, forecast_id, action, outcome, shares,
                    price, cash_delta, realized_pnl, reason, traded_at
                ) VALUES(?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?)
                """,
                (account_id, match_id, forecast_id, outcome, amount, price, proceeds, pnl, reason, now),
            )
            positions[outcome] = connection.execute(
                """
                SELECT * FROM paper_positions
                WHERE account_id=? AND match_id=? AND outcome=?
                """,
                (account_id, match_id, outcome),
            ).fetchone()
            actions.append(
                {"action": "SELL", "outcome": outcome, "shares": amount, "price": price, "reason": reason}
            )

        def buy(outcome: str, shares: float, reason: str) -> None:
            nonlocal cash
            if shares <= 1e-12:
                return
            price = ask[outcome]
            amount = min(shares, cash / price, remaining_risk_budget() / price)
            if amount <= 1e-12:
                return
            position = positions[outcome]
            old_shares = float(position["shares"])
            old_cost = old_shares * float(position["avg_cost"])
            cost = amount * price
            new_shares = old_shares + amount
            new_avg = (old_cost + cost) / new_shares
            cash -= cost
            connection.execute(
                """
                UPDATE paper_positions
                SET shares=?, avg_cost=?, updated_at=?
                WHERE account_id=? AND match_id=? AND outcome=?
                """,
                (new_shares, new_avg, now, account_id, match_id, outcome),
            )
            connection.execute(
                """
                INSERT INTO paper_trades(
                    account_id, match_id, forecast_id, action, outcome, shares,
                    price, cash_delta, realized_pnl, reason, traded_at
                ) VALUES(?, ?, ?, 'BUY', ?, ?, ?, ?, 0, ?, ?)
                """,
                (account_id, match_id, forecast_id, outcome, amount, price, -cost, reason, now),
            )
            positions[outcome] = connection.execute(
                """
                SELECT * FROM paper_positions
                WHERE account_id=? AND match_id=? AND outcome=?
                """,
                (account_id, match_id, outcome),
            ).fetchone()
            actions.append(
                {"action": "BUY", "outcome": outcome, "shares": amount, "price": price, "reason": reason}
            )

        # A side flip always closes the old outcome first.
        for outcome in ("A", "B"):
            current_shares = float(positions[outcome]["shares"])
            if current_shares <= 1e-12:
                continue
            if entry_side is not None and entry_side != outcome:
                sell(outcome, current_shares, "side_flip")
            elif probability[outcome] - bid[outcome] <= settings.exit_edge:
                sell(outcome, current_shares, "edge_gone")

        if entry_side is not None:
            target_cost = _kelly_cost(
                float(account["initial_cash"]),
                probability[entry_side],
                ask[entry_side],
                settings,
            )
            max_loss = float(account["initial_cash"]) * settings.max_match_fraction
            target_cost = min(target_cost, max(0.0, max_loss + match_realized_pnl()))
            target_shares = target_cost / ask[entry_side]
            current_shares = float(positions[entry_side]["shares"])
            if target_shares > current_shares + 1e-9:
                buy(entry_side, target_shares - current_shares, "entry_or_increase")
            elif current_shares > target_shares + 1e-9:
                sell(entry_side, current_shares - target_shares, "target_reduction")

        connection.execute(
            "UPDATE paper_accounts SET cash=?, updated_at=? WHERE account_id=?",
            (cash, now, account_id),
        )

    return actions


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
                    price, cash_delta, realized_pnl, reason, traded_at
                ) VALUES(?, ?, ?, 'SETTLE', ?, ?, ?, ?, ?, 'resolution', ?)
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
                    now,
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
