"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import websockets

from pmsim import __version__
from pmsim.analysis.metrics import edge_table
from pmsim.analysis.report import write_report
from pmsim.book.units import CASH_SCALE
from pmsim.config import Config, load_config
from pmsim.data.catalog import build_catalog, load_catalog, save_catalog, select_markets
from pmsim.data.gamma import GammaClient
from pmsim.data.http import HttpClient, JsonCache
from pmsim.data.recorder import ParquetSink, record
from pmsim.data.synthetic import SyntheticConfig
from pmsim.engine import state
from pmsim.engine.backtest import Backtest, BacktestConfig
from pmsim.engine.experiment import (
    DataSpec,
    RecordedSpec,
    RunSpec,
    run_full,
    run_one,
    stress_test,
)
from pmsim.engine.paper import PaperSink
from pmsim.engine.universe import MarketData, universe_from_catalog
from pmsim.strategies.registry import BUILDERS, build_strategy
from pmsim.ui.server import serve

DEFAULT_CONFIG = Path("config/default.yaml")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="pmsim", description="Polymarket simulator")
    parser.add_argument("--version", action="version", version=f"pmsim {__version__}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="YAML config path")
    sub = parser.add_subparsers(dest="command")

    fetch = sub.add_parser("fetch", help="fetch market metadata into the catalog")
    fetch.add_argument("--limit", type=int, default=500, help="max markets to fetch")
    fetch.add_argument("--closed", action="store_true", help="fetch closed markets instead")

    rec = sub.add_parser("record", help="record order books and trades to Parquet")
    rec.add_argument(
        "--markets", required=True, help="filter: slug substrings, tag:<slug>, neg_risk"
    )
    rec.add_argument("--hours", type=float, required=True, help="recording duration in hours")

    bt = sub.add_parser("backtest", help="run one strategy, optionally with stress tests")
    bt.add_argument("--strategy", required=True, choices=sorted(BUILDERS))
    bt.add_argument("--data", choices=["synthetic", "recorded"], default="synthetic")
    bt.add_argument("--seed", type=int, default=None, help="synthetic data seed")
    bt.add_argument("--hours", type=float, default=6.0, help="synthetic duration in hours")
    bt.add_argument("--params", default="{}", help="strategy parameters as JSON")
    bt.add_argument("--stress", action="store_true", help="also run the stress variants")
    bt.add_argument("--workers", type=int, default=1)
    bt.add_argument("--report", type=Path, default=None, help="write an HTML report here")

    pp = sub.add_parser("paper", help="paper trade a strategy on the live feed (no real orders)")
    pp.add_argument("--strategy", required=True, choices=sorted(BUILDERS))
    pp.add_argument("--markets", required=True, help="same filter as `record`")
    pp.add_argument("--hours", type=float, required=True)
    pp.add_argument("--params", default="{}", help="strategy parameters as JSON")
    pp.add_argument("--session", default="paper", help="name for the state and log files")

    ui = sub.add_parser("ui", help="open the local dashboard")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true", help="do not open a browser tab")

    ed = sub.add_parser("edges", help="theoretical vs realized edge for every strategy")
    ed.add_argument("--data", choices=["synthetic", "recorded"], default="synthetic")
    ed.add_argument("--seed", type=int, default=None)
    ed.add_argument("--hours", type=float, default=6.0)
    return parser


def _config(path: Path) -> Config:
    return load_config(path) if path.exists() else Config()


def cmd_fetch(cfg: Config, limit: int, closed: bool) -> int:
    cache = JsonCache(cfg.data_dir / "cache", cfg.api.cache_ttl_s)
    http = HttpClient(cfg.api, cache=cache)
    try:
        gamma = GammaClient(http, cfg.api.gamma_url)
        markets = list(gamma.iter_markets(limit, active=not closed, closed=closed))
    finally:
        http.close()
    path = save_catalog(build_catalog(markets), cfg.data_dir)
    print(f"saved {len(markets)} markets to {path}")
    return 0


def cmd_record(cfg: Config, spec: str, hours: float) -> int:
    try:
        catalog = load_catalog(cfg.data_dir)
    except FileNotFoundError:
        print("no catalog found; run `pmsim fetch` first", file=sys.stderr)
        return 2
    tokens = select_markets(catalog, spec)["token_id"].to_list()
    if not tokens:
        print(f"no open markets match {spec!r}", file=sys.stderr)
        return 2
    sink = ParquetSink(cfg.data_dir / "raw")
    print(f"recording {len(tokens)} tokens for {hours} h")
    asyncio.run(record(websockets.connect, cfg.api.ws_url, tokens, hours * 3600, sink))
    print(f"rows written: {sink.rows_written}")
    return 0


def _data_spec(cfg: Config, kind: str, seed: int | None, hours: float) -> DataSpec:
    if kind == "recorded":
        return RecordedSpec(str(cfg.data_dir))
    return SyntheticConfig(seed=cfg.seed if seed is None else seed, duration_hours=hours)


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    params = json.loads(args.params)
    data = _data_spec(cfg, args.data, args.seed, args.hours)
    bt = BacktestConfig(latency_ms=cfg.latency_ms, gas_per_trade_usd=cfg.fees.gas_per_trade_usd)
    if args.stress:
        report = stress_test(args.strategy, params, data, bt, args.workers)
        results = list(report.results)
    else:
        results = [run_one(RunSpec.make(args.strategy, params, data, backtest=bt))]
    if args.report is not None:
        full = run_full(RunSpec.make(args.strategy, params, data, backtest=bt))
        print(f"report written to {write_report(full, args.report)}")
    for r in results:
        print(
            f"[{r.label}] {r.strategy} {r.stress:11s} net PnL ${r.net_pnl_usd:,.2f}  "
            f"fills {r.fills}  expected edge ${r.expected_edge_usd:,.2f}  "
            f"leg failures {r.leg_failures}  rejects {r.rejects}"
        )
    if args.stress:
        verdict = "PASSED" if report.passed else "FAILED"
        print(f"stress test {verdict} ({report.label} data)")
    return 0


def cmd_paper(cfg: Config, args: argparse.Namespace) -> int:
    try:
        catalog = select_markets(load_catalog(cfg.data_dir), args.markets)
    except FileNotFoundError:
        print("no catalog found; run `pmsim fetch` first", file=sys.stderr)
        return 2
    if catalog.is_empty():
        print(f"no open markets match {args.markets!r}", file=sys.stderr)
        return 2
    universe = universe_from_catalog(catalog, cfg.fees)
    strategy = build_strategy(args.strategy, json.loads(args.params), universe)
    root = cfg.data_dir / "paper"
    state_path, log_path = root / f"{args.session}.state.json", root / f"{args.session}.log.jsonl"
    resumed = state.load(state_path)
    start = int(time.time() * 1000)
    if resumed is not None:
        resumed.now = max(resumed.now, start)
    bt_cfg = BacktestConfig(latency_ms=cfg.latency_ms, gas_per_trade_usd=cfg.fees.gas_per_trade_usd)
    bt = Backtest(strategy, MarketData(universe, []), bt_cfg, start_ts=start, portfolio=resumed)
    sink = PaperSink(bt, log_path, state_path)
    tokens = catalog["token_id"].to_list()
    word = "resuming" if resumed else "starting"
    print(f"{word} paper session {args.session!r}: {args.strategy} on {len(tokens)} tokens")
    asyncio.run(record(websockets.connect, cfg.api.ws_url, tokens, args.hours * 3600, sink))
    pf = bt.portfolio
    print(f"cash ${pf.cash / CASH_SCALE:,.2f}, realized ${pf.realized() / CASH_SCALE:,.2f}")
    print(f"state: {state_path}  log: {log_path}")
    return 0


def cmd_edges(cfg: Config, args: argparse.Namespace) -> int:
    data = _data_spec(cfg, args.data, args.seed, args.hours)
    bt = BacktestConfig(latency_ms=cfg.latency_ms, gas_per_trade_usd=cfg.fees.gas_per_trade_usd)
    results = [run_full(RunSpec.make(name, {}, data, backtest=bt)) for name in sorted(BUILDERS)]
    print(f"{'strategy':16s} {'data':10s} {'theoretical':>12s} {'realized':>12s} {'capture':>8s}")
    for row in edge_table(results):
        print(
            f"{row.strategy:16s} {row.label:10s} {row.theoretical_usd:12,.2f} "
            f"{row.realized_usd:12,.2f} {row.capture:8.2f}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return an exit code."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        return 0
    cfg = _config(args.config)
    if args.command == "fetch":
        return cmd_fetch(cfg, args.limit, args.closed)
    if args.command == "record":
        return cmd_record(cfg, args.markets, args.hours)
    if args.command == "ui":
        serve(cfg, args.host, args.port, not args.no_browser)
        return 0
    if args.command == "paper":
        return cmd_paper(cfg, args)
    if args.command == "edges":
        return cmd_edges(cfg, args)
    return cmd_backtest(cfg, args)
