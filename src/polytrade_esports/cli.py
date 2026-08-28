import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .collector import CollectorConfig, run_loop
from .dashboard import serve
from .engine import tick
from .gamma import GammaClient, parse_event
from .llm import (
    DEEPSEEK_API_KEY_ENV,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DeepSeekBackend,
    HermesCLIBackend,
)
from .paper import PaperConfig, settle_match
from .pandascore import PandaScoreClient
from .polymarket import PolymarketBookClient
from .priors import run_priors
from .resolver import resolve_open_matches
from .scoring import score
from .storage import Database
from .timeutil import isoformat, utc_now
from .types import BookQuote, LiveState, Match


DEFAULT_DB = os.environ.get("POLYTRADE_ESPORTS_DB", "data/esports_live.sqlite3")
PANDASCORE_TOKEN_ENV = "PANDASCORE_TOKEN"
PANDASCORE_ENABLED_ENV = "PANDASCORE_ENABLED"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("%s must be true or false" % name)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _state_from(args: argparse.Namespace, raw: Optional[Dict[str, Any]] = None) -> LiveState:
    return LiveState(
        match_id=args.match_id,
        source_at=args.source_at,
        observed_at=args.observed_at,
        maps_a=args.maps_a,
        maps_b=args.maps_b,
        rounds_a=args.rounds_a,
        rounds_b=args.rounds_b,
        current_map=args.current_map,
        side_advantage_a=args.side_advantage_a,
        economy_a=args.economy_a,
        economy_b=args.economy_b,
        map_bias_a=args.map_bias_a,
        source=args.state_source,
        raw=raw,
    )


def _paper_config(args: argparse.Namespace) -> PaperConfig:
    return PaperConfig(
        min_entry_edge=args.min_entry_edge,
        exit_edge=args.exit_edge,
        max_match_fraction=args.max_match_fraction,
        kelly_scale=args.kelly_scale,
    )


def _event_state(match_id: str, event: Dict[str, Any], source: str) -> LiveState:
    source_at = event["source_at"]
    return LiveState(
        match_id=match_id,
        source_at=source_at,
        observed_at=event.get("observed_at", source_at),
        maps_a=int(event["maps_a"]),
        maps_b=int(event["maps_b"]),
        rounds_a=int(event["rounds_a"]),
        rounds_b=int(event["rounds_b"]),
        current_map=str(event.get("current_map", "unknown")),
        side_advantage_a=float(event.get("side_advantage_a", 0.0)),
        economy_a=float(event.get("economy_a", 0.0)),
        economy_b=float(event.get("economy_b", 0.0)),
        map_bias_a=float(event.get("map_bias_a", 0.0)),
        source=source,
        raw=event,
    )


def _event_quote(match_id: str, event: Dict[str, Any], source: str) -> BookQuote:
    source_at = event.get("book_source_at", event["source_at"])
    return BookQuote(
        match_id=match_id,
        bid_a=float(event["bid_a"]),
        ask_a=float(event["ask_a"]),
        bid_b=float(event["bid_b"]),
        ask_b=float(event["ask_b"]),
        source_at=source_at,
        observed_at=event.get("book_observed_at", event.get("observed_at", source_at)),
        source=source,
        raw=event,
    )


def _read_jsonl(path: str) -> Iterable[Dict[str, Any]]:
    stream = sys.stdin if path == "-" else Path(path).open("r", encoding="utf-8")
    try:
        for line_number, line in enumerate(stream, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as error:
                raise ValueError("invalid JSON on line %d: %s" % (line_number, error))
            if not isinstance(value, dict):
                raise ValueError("line %d must contain a JSON object" % line_number)
            yield value
    finally:
        if stream is not sys.stdin:
            stream.close()


def cmd_init(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    account_id = database.ensure_account(args.account, args.initial_cash)
    _print({"database": args.db, "account": args.account, "account_id": account_id})


def cmd_add_match(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    match = Match(
        match_id=args.match_id,
        team_a=args.team_a,
        team_b=args.team_b,
        best_of=args.best_of,
        prior_probability_a=args.prior_a,
        token_a=args.token_a,
        token_b=args.token_b,
        source=args.source,
        external_id=args.external_id,
        scheduled_at=args.scheduled_at,
    )
    database.add_match(match)
    _print({"status": "ready", "match": match.__dict__})


def cmd_tick(args: argparse.Namespace) -> None:
    database = Database(args.db)
    state = _state_from(args)
    source_at = args.book_source_at or args.source_at
    quote = BookQuote(
        match_id=args.match_id,
        bid_a=args.bid_a,
        ask_a=args.ask_a,
        bid_b=args.bid_b,
        ask_b=args.ask_b,
        source_at=source_at,
        observed_at=args.book_observed_at or args.observed_at,
        source=args.book_source,
    )
    _print(tick(database, state, quote, args.account, _paper_config(args)))


def cmd_live_tick(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    match = database.get_match(args.match_id)
    state = _state_from(args)
    quote = PolymarketBookClient(timeout=args.timeout).get_pair(
        args.match_id, match.token_a, match.token_b
    )
    _print(tick(database, state, quote, args.account, _paper_config(args)))


def cmd_replay(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    results = []
    for event in _read_jsonl(args.path):
        results.append(
            tick(
                database,
                _event_state(args.match_id, event, args.source),
                _event_quote(args.match_id, event, args.source),
                args.account,
                _paper_config(args),
            )
        )
    _print({"ticks": len(results), "latest": results[-1] if results else None})


def cmd_stream(args: argparse.Namespace) -> None:
    """Consume normalized live state JSONL and emit one forecast per line."""
    database = Database(args.db)
    database.initialize()
    match = database.get_match(args.match_id)
    client = PolymarketBookClient(timeout=args.timeout)
    for event in _read_jsonl(args.path):
        observed_at = isoformat(utc_now())
        live_event = dict(event)
        live_event["observed_at"] = observed_at
        state = _event_state(args.match_id, live_event, args.source)
        if all(key in event for key in ("bid_a", "ask_a", "bid_b", "ask_b")):
            live_event["book_observed_at"] = observed_at
            quote = _event_quote(args.match_id, live_event, args.book_source)
        else:
            quote = client.get_pair(args.match_id, match.token_a, match.token_b)
        result = tick(database, state, quote, args.account, _paper_config(args))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


def cmd_demo(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    database.ensure_account(args.account, args.initial_cash)
    database.add_match(
        Match(
            match_id="demo-navi-m80",
            team_a="NAVI",
            team_b="M80",
            best_of=3,
            prior_probability_a=0.64,
            source="illustrative-replay",
        )
    )
    example = Path(__file__).with_name("demo_series.jsonl")
    results = []
    for event in _read_jsonl(str(example)):
        results.append(
            tick(
                database,
                _event_state("demo-navi-m80", event, "illustrative-replay"),
                _event_quote("demo-navi-m80", event, "illustrative-replay"),
                args.account,
                PaperConfig(),
            )
        )
    _print({"ticks": len(results), "database": args.db, "dashboard_port": 8788})


def cmd_fetch_book(args: argparse.Namespace) -> None:
    _print(PolymarketBookClient(timeout=args.timeout).get_book(args.token_id))


def cmd_resolve(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    resolved_at = args.resolved_at or isoformat(utc_now())
    forecast_id = database.latest_forecast_id(args.match_id)
    database.resolve_match(args.match_id, args.winner, resolved_at)
    actions = settle_match(database, args.account, args.match_id, args.winner, forecast_id)
    _print({"match_id": args.match_id, "winner": args.winner, "paper_actions": actions})


def cmd_status(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    _print(database.dashboard_payload(args.account))


def cmd_serve(args: argparse.Namespace) -> None:
    serve(Database(args.db), args.host, args.port, args.account)


def _collector_config(args: argparse.Namespace) -> CollectorConfig:
    return CollectorConfig(
        account_name=args.account,
        tick_window_hours=args.tick_window_hours,
        min_liquidity=args.min_liquidity,
        max_pages=args.max_pages,
        paper=_paper_config(args),
        pandascore_enabled=_env_flag(PANDASCORE_ENABLED_ENV, False),
        pandascore_token=os.environ.get(PANDASCORE_TOKEN_ENV, ""),
        sports_ws_enabled=args.sports_ws_enabled,
        sports_ws_url=args.sports_ws_url,
        sports_max_age_seconds=args.sports_max_age_seconds,
        sports_startup_wait_seconds=args.sports_startup_wait_seconds,
        max_match_age_hours=args.max_match_age_hours,
        resolve_every_cycles=args.resolve_every_cycles,
    )


def cmd_discover(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    client = GammaClient()
    seen = 0
    inserted = 0
    updated = 0
    conflicts = 0
    preview = []
    for event in client.cs2_events(max_pages=args.max_pages):
        record = parse_event(event)
        if record is None or not record["match_id"]:
            continue
        seen += 1
        if args.dry_run:
            preview.append(
                {
                    "match_id": record["match_id"],
                    "team_a": record["team_a"],
                    "team_b": record["team_b"],
                    "best_of": record["best_of"],
                    "live": record["live"],
                    "liquidity": record["liquidity"],
                    "scheduled_at": record["scheduled_at"],
                    "pandascore_match_id": record["pandascore_match_id"],
                }
            )
            continue
        outcome = database.upsert_discovered_match(record, 0.5)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            conflicts += 1
    _print(
        {
            "discovered": seen,
            "inserted": inserted,
            "updated": updated,
            "conflicts": conflicts,
            "dry_run": args.dry_run,
            "matches": preview[: args.limit] if args.dry_run else [],
        }
    )


def cmd_collect(args: argparse.Namespace) -> None:
    database = Database(args.db)
    config = _collector_config(args)
    if args.cycles == 1:
        completed = []
        run_loop(
            database=database,
            config=config,
            interval_seconds=args.interval_seconds,
            cycles=1,
            on_cycle=completed.append,
        )
        _print(completed[0].to_dict())
        return
    run_loop(
        database=database,
        config=config,
        interval_seconds=args.interval_seconds,
        cycles=args.cycles,
        on_cycle=lambda result: print(
            json.dumps(
                {
                    "cycle_at": isoformat(utc_now()),
                    "discovered": result.discovered,
                    "inserted": result.inserted,
                    "ticked": result.ticked,
                    "skipped": result.skipped,
                    "finished": result.finished,
                    "errors": result.errors[:5],
                },
                ensure_ascii=False,
            ),
            flush=True,
        ),
    )


def _prior_backend(args: argparse.Namespace) -> Any:
    if args.backend == "hermes":
        return HermesCLIBackend(
            hermes_bin=args.hermes_bin,
            model=args.model,
            provider=args.provider,
            timeout_seconds=args.timeout,
        )
    api_key = args.api_key or os.environ.get(DEEPSEEK_API_KEY_ENV, "")
    if not api_key:
        raise ValueError(
            "no DeepSeek API key: pass --api-key or set %s" % DEEPSEEK_API_KEY_ENV
        )
    return DeepSeekBackend(
        api_key=api_key,
        model=args.model,
        timeout_seconds=args.timeout,
    )


def cmd_forecast_priors(args: argparse.Namespace) -> None:
    database = Database(args.db)
    backend = _prior_backend(args)
    if args.loop_seconds > 0:
        _loop_priors(database, backend, args)
        return
    summary, created = run_priors(
        database=database,
        backend=backend,
        limit=args.limit,
        daily_limit=args.daily_limit,
        monthly_budget_usd=args.monthly_budget_usd,
        max_cost_per_forecast=args.max_cost_per_forecast,
        min_liquidity=args.min_liquidity,
        provider=args.provider,
        model=args.model,
        dry_run=args.dry_run,
        backend_name=args.backend,
        require_facts=not args.allow_ungrounded,
    )
    _print({"summary": summary, "priors": created})


def cmd_resolve_open(args: argparse.Namespace) -> None:
    database = Database(args.db)
    result = resolve_open_matches(
        database=database,
        account_name=args.account,
        min_age_hours=args.min_age_hours,
        limit=args.limit,
    )
    _print(result.to_dict())


def cmd_score(args: argparse.Namespace) -> None:
    database = Database(args.db)
    database.initialize()
    report = score(database)
    if args.summary:
        report.pop("matches", None)
    _print(report)


def _loop_priors(database: Database, backend: Any, args: argparse.Namespace) -> None:
    """Price new matches on an interval, so coverage keeps up with discovery.

    A match only needs one prior, so a run that finds nothing new is the normal
    steady state and costs a single database query, not an API call.
    """
    while True:
        try:
            summary, created = run_priors(
                database=database,
                backend=backend,
                limit=args.limit,
                daily_limit=args.daily_limit,
                monthly_budget_usd=args.monthly_budget_usd,
                max_cost_per_forecast=args.max_cost_per_forecast,
                min_liquidity=args.min_liquidity,
                provider=args.provider,
                model=args.model,
                dry_run=False,
                backend_name=args.backend,
                require_facts=not args.allow_ungrounded,
            )
            print(
                json.dumps(
                    {
                        "at": isoformat(utc_now()),
                        "priced": summary["priors_created"],
                        "candidates": summary["candidates_selected"],
                        "month_cost_usd": round(
                            summary["month_cost_before_run_usd"]
                            + float(summary["usage"].get("estimated_cost_usd", 0.0)),
                            4,
                        ),
                        "errors": summary["errors"][:3],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as error:  # keep the loop alive across transient faults
            print(
                json.dumps({"at": isoformat(utc_now()), "error": str(error)[:300]}),
                flush=True,
            )
        time.sleep(max(30.0, float(args.loop_seconds)))


def cmd_pandascore_probe(args: argparse.Namespace) -> None:
    token = args.token or os.environ.get(PANDASCORE_TOKEN_ENV, "")
    if not token:
        raise ValueError(
            "no PandaScore token: pass --token or set %s" % PANDASCORE_TOKEN_ENV
        )
    _print(PandaScoreClient(token).probe())


def _add_paper_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account", default="live-paper")
    parser.add_argument("--min-entry-edge", type=float, default=0.10)
    parser.add_argument("--exit-edge", type=float, default=0.00)
    parser.add_argument("--max-match-fraction", type=float, default=0.01)
    parser.add_argument("--kelly-scale", type=float, default=0.25)


def _add_state_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--source-at", required=True)
    parser.add_argument("--observed-at")
    parser.add_argument("--maps-a", type=int, required=True)
    parser.add_argument("--maps-b", type=int, required=True)
    parser.add_argument("--rounds-a", type=int, required=True)
    parser.add_argument("--rounds-b", type=int, required=True)
    parser.add_argument("--current-map", default="unknown")
    parser.add_argument("--side-advantage-a", type=float, default=0.0)
    parser.add_argument("--economy-a", type=float, default=0.0)
    parser.add_argument("--economy-b", type=float, default=0.0)
    parser.add_argument("--map-bias-a", type=float, default=0.0)
    parser.add_argument("--state-source", default="manual")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CS2 live probability research, paper only")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--db", default=DEFAULT_DB)
    init.add_argument("--account", default="live-paper")
    init.add_argument("--initial-cash", type=float, default=1000.0)
    init.set_defaults(func=cmd_init)

    add = subparsers.add_parser("add-match")
    add.add_argument("--db", default=DEFAULT_DB)
    add.add_argument("--match-id", required=True)
    add.add_argument("--team-a", required=True)
    add.add_argument("--team-b", required=True)
    add.add_argument("--best-of", type=int, choices=(1, 3, 5), required=True)
    add.add_argument("--prior-a", type=float, required=True)
    add.add_argument("--token-a", default="")
    add.add_argument("--token-b", default="")
    add.add_argument("--source", default="manual")
    add.add_argument("--external-id", default="")
    add.add_argument("--scheduled-at")
    add.set_defaults(func=cmd_add_match)

    manual_tick = subparsers.add_parser("tick")
    manual_tick.add_argument("--db", default=DEFAULT_DB)
    _add_state_arguments(manual_tick)
    manual_tick.add_argument("--bid-a", type=float, required=True)
    manual_tick.add_argument("--ask-a", type=float, required=True)
    manual_tick.add_argument("--bid-b", type=float, required=True)
    manual_tick.add_argument("--ask-b", type=float, required=True)
    manual_tick.add_argument("--book-source-at")
    manual_tick.add_argument("--book-observed-at")
    manual_tick.add_argument("--book-source", default="manual")
    _add_paper_arguments(manual_tick)
    manual_tick.set_defaults(func=cmd_tick)

    live_tick = subparsers.add_parser("live-tick")
    live_tick.add_argument("--db", default=DEFAULT_DB)
    _add_state_arguments(live_tick)
    live_tick.add_argument("--timeout", type=float, default=10.0)
    _add_paper_arguments(live_tick)
    live_tick.set_defaults(func=cmd_live_tick)

    replay = subparsers.add_parser("replay")
    replay.add_argument("path", help="JSONL path or - for stdin")
    replay.add_argument("--db", default=DEFAULT_DB)
    replay.add_argument("--match-id", required=True)
    replay.add_argument("--source", default="normalized-jsonl-replay")
    _add_paper_arguments(replay)
    replay.set_defaults(func=cmd_replay)

    stream = subparsers.add_parser("stream")
    stream.add_argument("path", nargs="?", default="-", help="live JSONL path or - for stdin")
    stream.add_argument("--db", default=DEFAULT_DB)
    stream.add_argument("--match-id", required=True)
    stream.add_argument("--source", default="normalized-live-feed")
    stream.add_argument("--book-source", default="normalized-live-book")
    stream.add_argument("--timeout", type=float, default=10.0)
    _add_paper_arguments(stream)
    stream.set_defaults(func=cmd_stream)

    demo = subparsers.add_parser("demo")
    demo.add_argument("--db", default=DEFAULT_DB)
    demo.add_argument("--account", default="live-paper")
    demo.add_argument("--initial-cash", type=float, default=1000.0)
    demo.set_defaults(func=cmd_demo)

    fetch = subparsers.add_parser("fetch-book")
    fetch.add_argument("--token-id", required=True)
    fetch.add_argument("--timeout", type=float, default=10.0)
    fetch.set_defaults(func=cmd_fetch_book)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--db", default=DEFAULT_DB)
    resolve.add_argument("--match-id", required=True)
    resolve.add_argument("--winner", choices=("A", "B"), required=True)
    resolve.add_argument("--resolved-at")
    resolve.add_argument("--account", default="live-paper")
    resolve.set_defaults(func=cmd_resolve)

    status = subparsers.add_parser("status")
    status.add_argument("--db", default=DEFAULT_DB)
    status.add_argument("--account", default="live-paper")
    status.set_defaults(func=cmd_status)

    dashboard = subparsers.add_parser("serve")
    dashboard.add_argument("--db", default=DEFAULT_DB)
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8788)
    dashboard.add_argument("--account", default="live-paper")
    dashboard.set_defaults(func=cmd_serve)

    discover = subparsers.add_parser(
        "discover", help="find open CS2 match markets on Polymarket"
    )
    discover.add_argument("--db", default=DEFAULT_DB)
    discover.add_argument("--max-pages", type=int, default=6)
    discover.add_argument("--limit", type=int, default=20)
    discover.add_argument("--dry-run", action="store_true")
    discover.set_defaults(func=cmd_discover)

    collect = subparsers.add_parser(
        "collect", help="discover, read live state and books, forecast"
    )
    collect.add_argument("--db", default=DEFAULT_DB)
    collect.add_argument("--interval-seconds", type=float, default=60.0)
    collect.add_argument("--cycles", type=int, default=1)
    collect.add_argument("--tick-window-hours", type=float, default=3.0)
    collect.add_argument("--min-liquidity", type=float, default=0.0)
    collect.add_argument("--max-pages", type=int, default=6)
    collect.add_argument("--max-match-age-hours", type=float, default=12.0)
    collect.add_argument("--resolve-every-cycles", type=int, default=30)
    collect.add_argument(
        "--no-sports-ws",
        action="store_false",
        dest="sports_ws_enabled",
        help="disable the Polymarket Sports WebSocket round feed",
    )
    collect.add_argument(
        "--sports-ws-url",
        default="wss://sports-api.polymarket.com/ws",
    )
    collect.add_argument("--sports-max-age-seconds", type=float, default=90.0)
    collect.add_argument("--sports-startup-wait-seconds", type=float, default=5.0)
    _add_paper_arguments(collect)
    collect.set_defaults(func=cmd_collect, sports_ws_enabled=True)

    priors = subparsers.add_parser(
        "forecast-priors", help="LLM pre-match priors for un-priced matches"
    )
    priors.add_argument("--db", default=DEFAULT_DB)
    priors.add_argument("--limit", type=int, default=5)
    priors.add_argument("--daily-limit", type=int, default=40)
    priors.add_argument("--monthly-budget-usd", type=float, default=6.0)
    priors.add_argument("--max-cost-per-forecast", type=float, default=0.10)
    priors.add_argument("--min-liquidity", type=float, default=0.0)
    priors.add_argument(
        "--backend",
        choices=("deepseek", "hermes"),
        default="deepseek",
        help="deepseek: direct API, cheap, no web research. hermes: CLI with web research",
    )
    priors.add_argument("--api-key", default="", help="overrides $" + DEEPSEEK_API_KEY_ENV)
    priors.add_argument("--model", default=DEFAULT_MODEL)
    priors.add_argument("--provider", default=DEFAULT_PROVIDER)
    priors.add_argument("--hermes-bin", default="/usr/local/bin/hermes")
    priors.add_argument("--timeout", type=float, default=300.0)
    priors.add_argument(
        "--allow-ungrounded",
        action="store_true",
        help="write a prior even when no verified team facts could be fetched",
    )
    priors.add_argument("--dry-run", action="store_true")
    priors.add_argument(
        "--loop-seconds",
        type=float,
        default=0.0,
        help="run continuously at this interval instead of once",
    )
    priors.set_defaults(func=cmd_forecast_priors)

    resolve_open = subparsers.add_parser(
        "resolve-open", help="settle finished matches from the Polymarket result"
    )
    resolve_open.add_argument("--db", default=DEFAULT_DB)
    resolve_open.add_argument("--account", default="live-paper")
    resolve_open.add_argument("--min-age-hours", type=float, default=5.0)
    resolve_open.add_argument("--limit", type=int, default=60)
    resolve_open.set_defaults(func=cmd_resolve_open)

    scoring = subparsers.add_parser(
        "score", help="Brier and log loss for the AI prior against the market"
    )
    scoring.add_argument("--db", default=DEFAULT_DB)
    scoring.add_argument("--summary", action="store_true", help="omit per-match rows")
    scoring.set_defaults(func=cmd_score)

    probe = subparsers.add_parser(
        "pandascore-probe", help="report what a PandaScore token can read"
    )
    probe.add_argument("--token", default="")
    probe.set_defaults(func=cmd_pandascore_probe)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        args.func(args)
    except (KeyError, ValueError) as error:
        parser.error(str(error))
