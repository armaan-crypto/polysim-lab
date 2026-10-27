"""Replay recorded market data as a time-ordered event stream."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from pmsim.book.l2 import L2Book, Level
from pmsim.book.units import Side, price_units, size_units


@dataclass(frozen=True)
class BookSnapshot:
    token: str
    ts: int
    recv_ts: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass(frozen=True)
class BookDelta:
    token: str
    ts: int
    recv_ts: int
    side: Side
    price: int
    size: int


@dataclass(frozen=True)
class TradePrint:
    token: str
    ts: int
    recv_ts: int
    price: int
    size: int


@dataclass(frozen=True)
class TickChange:
    token: str
    ts: int
    recv_ts: int
    tick: int


MarketEvent = BookSnapshot | BookDelta | TradePrint | TickChange

# Same timestamp: the snapshot first, then trades, then the deltas they cause.
_PRIORITY = {BookSnapshot: 0, TickChange: 1, TradePrint: 2, BookDelta: 3}


def event_key(ev: MarketEvent) -> tuple[int, int, int]:
    return ev.ts, ev.recv_ts, _PRIORITY[type(ev)]


def _scan(raw_dir: Path, table: str, tokens: set[str] | None) -> pl.DataFrame | None:
    files = sorted((raw_dir / table).glob("*.parquet"))
    if not files:
        return None
    df = pl.concat([pl.read_parquet(f) for f in files])
    return df if tokens is None else df.filter(pl.col("asset_id").is_in(tokens))


def _snapshots(df: pl.DataFrame) -> Iterator[BookSnapshot]:
    keys = ["asset_id", "exchange_ts_ms", "recv_ts_ms", "hash"]
    for (token, ts, recv, _), grp in df.group_by(keys, maintain_order=True):
        lv = grp.sort("level")
        side = {
            s: tuple(
                (price_units(p), size_units(q))
                for p, q in lv.filter(pl.col("side") == s).select("price", "size").iter_rows()
            )
            for s in ("BUY", "SELL")
        }
        yield BookSnapshot(str(token), int(ts), int(recv), side["BUY"], side["SELL"])


def _deltas(df: pl.DataFrame) -> Iterator[BookDelta]:
    cols = ["asset_id", "exchange_ts_ms", "recv_ts_ms", "side", "price", "size"]
    for token, ts, recv, side, p, q in df.select(cols).iter_rows():
        yield BookDelta(token, ts, recv, side, price_units(p), size_units(q))


def _trades(df: pl.DataFrame) -> Iterator[TradePrint]:
    cols = ["asset_id", "exchange_ts_ms", "recv_ts_ms", "price", "size"]
    for token, ts, recv, p, q in df.select(cols).iter_rows():
        yield TradePrint(token, ts, recv, price_units(p), size_units(q))


def _ticks(df: pl.DataFrame) -> Iterator[TickChange]:
    cols = ["asset_id", "exchange_ts_ms", "recv_ts_ms", "new_tick_size"]
    for token, ts, recv, tick in df.select(cols).iter_rows():
        yield TickChange(token, ts, recv, price_units(tick))


def load_events(raw_dir: Path, tokens: set[str] | None = None) -> list[MarketEvent]:
    """Read recorder output under `raw_dir` and return events in replay order."""
    readers: dict[str, Callable[[pl.DataFrame], Iterator[MarketEvent]]] = {
        "book_snapshots": _snapshots,
        "book_deltas": _deltas,
        "trades": _trades,
        "tick_size_changes": _ticks,
    }
    events: list[MarketEvent] = []
    for table, reader in readers.items():
        df = _scan(raw_dir, table, tokens)
        if df is not None:
            events.extend(reader(df))
    events.sort(key=event_key)
    return events


def rebuild_books(events: Iterable[MarketEvent]) -> dict[str, L2Book]:
    """Apply book events in order and return the final book per token."""
    books: dict[str, L2Book] = {}
    for ev in events:
        if isinstance(ev, BookSnapshot):
            books.setdefault(ev.token, L2Book()).apply_snapshot(ev.bids, ev.asks)
        elif isinstance(ev, BookDelta):
            books.setdefault(ev.token, L2Book()).apply_delta(ev.side, ev.price, ev.size)
    return books
