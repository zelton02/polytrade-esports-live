from typing import Any, Dict, Optional

from .paper import PaperConfig, rebalance
from .probability import live_probability
from .storage import Database
from .state_guard import validate_strategy
from .timeutil import canonical_timestamp, isoformat, utc_now
from .types import BookQuote, LiveState


def tick(
    database: Database,
    state: LiveState,
    quote: BookQuote,
    account_name: str = "live-paper",
    paper_config: Optional[PaperConfig] = None,
    paper_enabled: bool = True,
    entry_enabled: bool = True,
    strategy: str = "pre-match",
    decision_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Record one state/book observation, forecast, and optionally rebalance.

    ``paper_enabled=False`` still stores the forecast but takes no position. The
    caller uses it when the match has no researched prior: a model sitting at
    the neutral seed shows a large apparent edge against any confident market
    price, and acting on that would be trading on ignorance.
    """
    database.initialize()
    strategy = validate_strategy(strategy)
    normalized_state = state.normalized()
    normalized_quote = quote.normalized()
    if normalized_state.match_id != normalized_quote.match_id:
        raise ValueError("state and quote must reference the same match")
    match = database.get_match(normalized_state.match_id)
    breakdown = live_probability(match, normalized_state)
    probability_a = breakdown.live_series_probability_a
    edge_a = probability_a - normalized_quote.ask_a
    edge_b = (1.0 - probability_a) - normalized_quote.ask_b
    best_edge = max(edge_a, edge_b)
    best_side = None
    if best_edge > 0:
        best_side = "A" if edge_a >= edge_b else "B"
    forecast_at = canonical_timestamp(decision_at or isoformat(utc_now()))
    state_id = database.record_state(normalized_state)
    book_id = database.record_book(normalized_quote)
    forecast_id = database.record_forecast(
        match_id=match.match_id,
        state_id=state_id,
        book_id=book_id,
        forecast_at=forecast_at,
        model_version=breakdown.model_version,
        probability_a=probability_a,
        market_midpoint_a=normalized_quote.midpoint_a,
        edge_a=edge_a,
        edge_b=edge_b,
        best_side=best_side,
        breakdown=breakdown.to_dict(),
        strategy=strategy,
        paper_enabled=paper_enabled,
        entry_enabled=paper_enabled and entry_enabled,
        execution_mode="depth-sim",
    )
    # Drift since our own view last had reason to change.
    anchor_market = database.market_at_last_state_change(
        match.match_id, normalized_state
    )
    market_drift = (
        None if anchor_market is None
        else normalized_quote.midpoint_a - anchor_market
    )
    actions = (
        rebalance(
            database=database,
            account_name=account_name,
            forecast_id=forecast_id,
            match_id=match.match_id,
            probability_a=probability_a,
            quote=normalized_quote,
            config=paper_config,
            market_drift=market_drift,
            entry_enabled=entry_enabled,
            strategy=strategy,
            signal_at=forecast_at,
        )
        if paper_enabled
        else []
    )
    return {
        "forecast_id": forecast_id,
        "match_id": match.match_id,
        "team_a": match.team_a,
        "team_b": match.team_b,
        "probability_a": probability_a,
        "probability_b": 1.0 - probability_a,
        "market_midpoint_a": normalized_quote.midpoint_a,
        "edge_a": edge_a,
        "edge_b": edge_b,
        "best_side": best_side,
        "paper_actions": actions,
        "paper_enabled": paper_enabled,
        "entry_enabled": paper_enabled and entry_enabled,
        "strategy": strategy,
        "market_drift": market_drift,
        "breakdown": breakdown.to_dict(),
        "forecast_at": forecast_at,
    }
