"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import websockets

from pmsim import __version__
from pmsim.config import Config, load_config
from pmsim.data.catalog import build_catalog, load_catalog, save_catalog, select_markets
from pmsim.data.gamma import GammaClient
from pmsim.data.http import HttpClient, JsonCache
from pmsim.data.recorder import ParquetSink, record

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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return an exit code."""
    args = build_parser().parse_args(argv)
    if args.command is None:
        return 0
    cfg = _config(args.config)
    if args.command == "fetch":
        return cmd_fetch(cfg, args.limit, args.closed)
    return cmd_record(cfg, args.markets, args.hours)
