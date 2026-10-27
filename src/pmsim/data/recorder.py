"""WebSocket recorder: market channel events to Parquet.

Every row carries the exchange timestamp (`exchange_ts_ms`) and the local
receive timestamp (`recv_ts_ms`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol

import polars as pl
from websockets.exceptions import WebSocketException

log = logging.getLogger(__name__)

PING_INTERVAL_S = 10.0  # documented application-level heartbeat
Row = dict[str, object]
Rows = dict[str, list[Row]]

_KEY = {"asset_id": pl.Utf8, "market": pl.Utf8, "exchange_ts_ms": pl.Int64, "recv_ts_ms": pl.Int64}
TABLES: dict[str, Mapping[str, pl.DataType | type[pl.DataType]]] = {
    "book_snapshots": {
        **_KEY,
        "hash": pl.Utf8,
        "side": pl.Utf8,
        "level": pl.Int32,
        "price": pl.Float64,
        "size": pl.Float64,
    },
    "book_deltas": {
        **_KEY,
        "hash": pl.Utf8,
        "side": pl.Utf8,
        "price": pl.Float64,
        "size": pl.Float64,
        "best_bid": pl.Float64,
        "best_ask": pl.Float64,
    },
    "trades": {
        **_KEY,
        "side": pl.Utf8,
        "price": pl.Float64,
        "size": pl.Float64,
        "fee_rate_bps": pl.Float64,
        "transaction_hash": pl.Utf8,
    },
    "tick_size_changes": {**_KEY, "old_tick_size": pl.Float64, "new_tick_size": pl.Float64},
}


def _opt_float(value: object) -> float | None:
    return None if value in (None, "") else float(str(value))


def _key(ev: Mapping[str, Any], asset_id: str, recv_ts_ms: int) -> Row:
    return {
        "asset_id": asset_id,
        "market": ev.get("market", ""),
        "exchange_ts_ms": int(ev["timestamp"]),
        "recv_ts_ms": recv_ts_ms,
    }


def _book_rows(ev: Mapping[str, Any], recv: int) -> list[Row]:
    """Levels numbered from the best price (level 0). Bids are BUY, asks SELL."""
    key = _key(ev, ev["asset_id"], recv)
    rows: list[Row] = []
    for side, levels in (("BUY", ev["bids"]), ("SELL", ev["asks"])):
        best_first = sorted(levels, key=lambda lv: float(lv["price"]), reverse=side == "BUY")
        for i, lv in enumerate(best_first):
            rows.append(
                {
                    **key,
                    "hash": ev.get("hash", ""),
                    "side": side,
                    "level": i,
                    "price": float(lv["price"]),
                    "size": float(lv["size"]),
                }
            )
    return rows


def _delta_rows(ev: Mapping[str, Any], recv: int) -> list[Row]:
    return [
        {
            **_key(ev, ch["asset_id"], recv),
            "hash": ch.get("hash", ""),
            "side": ch["side"],
            "price": float(ch["price"]),
            "size": float(ch["size"]),
            "best_bid": _opt_float(ch.get("best_bid")),
            "best_ask": _opt_float(ch.get("best_ask")),
        }
        for ch in ev["price_changes"]
    ]


def _trade_rows(ev: Mapping[str, Any], recv: int) -> list[Row]:
    return [
        {
            **_key(ev, ev["asset_id"], recv),
            "side": ev["side"],
            "price": float(ev["price"]),
            "size": float(ev["size"]),
            "fee_rate_bps": _opt_float(ev.get("fee_rate_bps")),
            "transaction_hash": ev.get("transaction_hash", ""),
        }
    ]


def _tick_rows(ev: Mapping[str, Any], recv: int) -> list[Row]:
    return [
        {
            **_key(ev, ev["asset_id"], recv),
            "old_tick_size": float(ev["old_tick_size"]),
            "new_tick_size": float(ev["new_tick_size"]),
        }
    ]


_PARSERS: dict[str, tuple[str, Callable[[Mapping[str, Any], int], list[Row]]]] = {
    "book": ("book_snapshots", _book_rows),
    "price_change": ("book_deltas", _delta_rows),
    "last_trade_price": ("trades", _trade_rows),
    "tick_size_change": ("tick_size_changes", _tick_rows),
}


def parse_message(raw: str, recv_ts_ms: int) -> Rows:
    """Split one WebSocket frame (an event or a list of events) into table rows.

    Heartbeat replies and unknown event types produce no rows.
    """
    out: Rows = {}
    if not raw.lstrip().startswith(("{", "[")):
        return out
    decoded = json.loads(raw)
    for ev in decoded if isinstance(decoded, list) else [decoded]:
        parser = _PARSERS.get(ev.get("event_type", ""))
        if parser is None:
            continue
        table, fn = parser
        out.setdefault(table, []).extend(fn(ev, recv_ts_ms))
    return out


class ParquetSink:
    """Buffers rows per table and writes Parquet parts under `root/<table>/`."""

    def __init__(self, root: Path, flush_rows: int = 50_000) -> None:
        self._root = root
        self._flush_rows = flush_rows
        self._buf: Rows = {t: [] for t in TABLES}
        self._parts = 0
        self.rows_written: dict[str, int] = dict.fromkeys(TABLES, 0)

    def add(self, rows: Rows) -> None:
        for table, new in rows.items():
            self._buf[table].extend(new)
        if sum(len(b) for b in self._buf.values()) >= self._flush_rows:
            self.flush()

    def flush(self) -> None:
        for table, rows in self._buf.items():
            if not rows:
                continue
            directory = self._root / table
            directory.mkdir(parents=True, exist_ok=True)
            self._parts += 1
            name = f"part-{int(time.time() * 1000)}-{self._parts:05d}.parquet"
            pl.DataFrame(rows, schema=dict(TABLES[table])).write_parquet(directory / name)
            self.rows_written[table] += len(rows)
            self._buf[table] = []


class Socket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


Connect = Callable[[str], AbstractAsyncContextManager[Socket]]


def subscribe_frame(asset_ids: list[str]) -> str:
    return json.dumps({"assets_ids": asset_ids, "type": "market"})


async def _pinger(ws: Socket) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        await ws.send("PING")


async def _frames(ws: Socket, deadline: float) -> AsyncIterator[str]:
    while (left := deadline - time.monotonic()) > 0:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
        except TimeoutError:
            return
        yield raw.decode() if isinstance(raw, bytes) else raw


async def _session(
    connect: Connect, url: str, asset_ids: list[str], sink: ParquetSink, deadline: float
) -> None:
    async with connect(url) as ws:
        await ws.send(subscribe_frame(asset_ids))
        ping = asyncio.create_task(_pinger(ws))
        try:
            async for raw in _frames(ws, deadline):
                sink.add(parse_message(raw, int(time.time() * 1000)))
        finally:
            ping.cancel()


async def record(
    connect: Connect,
    url: str,
    asset_ids: list[str],
    duration_s: float,
    sink: ParquetSink,
    max_reconnects: int = 20,
    backoff_s: float = 1.0,
) -> None:
    """Record until `duration_s` elapses, reconnecting with backoff on drops."""
    deadline = time.monotonic() + duration_s
    failures = 0
    try:
        while time.monotonic() < deadline:
            try:
                await _session(connect, url, asset_ids, sink, deadline)
                failures = 0
            except (OSError, WebSocketException) as exc:
                failures += 1
                if failures > max_reconnects:
                    raise
                log.warning("websocket dropped (%r); reconnect %d", exc, failures)
                await asyncio.sleep(min(backoff_s * 2 ** (failures - 1), 30.0))
    finally:
        sink.flush()
