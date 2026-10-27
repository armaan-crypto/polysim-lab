import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import polars as pl
import pytest

from conftest import ws_frames
from pmsim.data import recorder
from pmsim.data.recorder import TABLES, ParquetSink, Socket, parse_message, record

# Example frames copied from the official market-channel docs (no trade or
# tick change occurred during the recorded sample).
DOC_TRADE = {
    "event_type": "last_trade_price",
    "market": "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75",
    "asset_id": "107505882767731489358349912513945399560393482969656700824895970500493757150417",
    "price": "0.08",
    "size": "219.217767",
    "fee_rate_bps": "0",
    "side": "SELL",
    "timestamp": "1782753357257",
    "transaction_hash": "0xeeefff",
}
DOC_TICK = {
    "event_type": "tick_size_change",
    "market": "0x747d",
    "asset_id": "1075",
    "old_tick_size": "0.01",
    "new_tick_size": "0.001",
    "timestamp": "1782753357257",
}


def test_recorded_book_frame_levels_best_first() -> None:
    rows = parse_message(ws_frames()[0], recv_ts_ms=1)["book_snapshots"]
    rows = [r for r in rows if r["asset_id"] == rows[0]["asset_id"]]
    bids = [r for r in rows if r["side"] == "BUY"]
    asks = [r for r in rows if r["side"] == "SELL"]
    assert bids[0]["level"] == 0 and asks[0]["level"] == 0
    assert [r["price"] for r in bids] == sorted((r["price"] for r in bids), reverse=True)
    assert [r["price"] for r in asks] == sorted(r["price"] for r in asks)
    assert bids[0]["price"] < asks[0]["price"]  # type: ignore[operator]
    assert all(r["recv_ts_ms"] == 1 and r["exchange_ts_ms"] > 0 for r in rows)  # type: ignore[operator]


def test_recorded_price_change_frames() -> None:
    deltas = [r for f in ws_frames() for r in parse_message(f, 7).get("book_deltas", [])]
    assert deltas
    assert {r["side"] for r in deltas} <= {"BUY", "SELL"}
    assert all(isinstance(r["best_bid"], float) for r in deltas)


def test_doc_trade_and_tick_frames() -> None:
    trade = parse_message(json.dumps(DOC_TRADE), 5)["trades"][0]
    assert trade["price"] == 0.08 and trade["side"] == "SELL" and trade["fee_rate_bps"] == 0.0
    assert trade["exchange_ts_ms"] == 1782753357257
    tick = parse_message(json.dumps(DOC_TICK), 5)["tick_size_changes"][0]
    assert tick["new_tick_size"] == 0.001


def test_heartbeat_and_unknown_events_ignored() -> None:
    assert parse_message("PONG", 1) == {}
    assert parse_message(json.dumps({"event_type": "best_bid_ask"}), 1) == {}


def test_sink_writes_typed_parquet(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path, flush_rows=10_000)
    for frame in ws_frames():
        sink.add(parse_message(frame, 3))
    sink.add(parse_message(json.dumps(DOC_TRADE), 3))
    sink.flush()
    snaps = pl.read_parquet(tmp_path / "book_snapshots")
    assert dict(snaps.schema) == dict(TABLES["book_snapshots"])
    assert pl.read_parquet(tmp_path / "trades").height == 1
    assert sink.rows_written["book_deltas"] > 0
    assert not (tmp_path / "tick_size_changes").exists()


def test_sink_flushes_at_threshold(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path, flush_rows=1)
    sink.add(parse_message(json.dumps(DOC_TRADE), 3))
    assert len(list((tmp_path / "trades").iterdir())) == 1


class FakeSocket:
    def __init__(self, frames: list[str], fail_after: bool) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.fail_after = fail_after

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        if self.fail_after:
            raise ConnectionResetError("dropped")
        await asyncio.sleep(3600)
        raise AssertionError


def test_record_reconnects_and_flushes(tmp_path: Path) -> None:
    frames = ws_frames()[:3]
    sockets = [FakeSocket(frames, fail_after=True), FakeSocket(frames, fail_after=False)]
    used: list[FakeSocket] = []

    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[Socket]:
        sock = sockets[len(used)]
        used.append(sock)
        yield sock

    sink = ParquetSink(tmp_path)
    asyncio.run(record(connect, "wss://x", ["a", "b"], 0.3, sink, backoff_s=0))
    assert len(used) == 2
    assert json.loads(used[0].sent[0]) == {"assets_ids": ["a", "b"], "type": "market"}
    one_pass = sum(len(r) for f in frames for r in parse_message(f, 0).values())
    assert sum(sink.rows_written.values()) == 2 * one_pass


def test_record_gives_up_after_max_reconnects(tmp_path: Path) -> None:
    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[Socket]:
        raise OSError("refused")
        yield FakeSocket([], False)  # pragma: no cover

    with pytest.raises(OSError):
        asyncio.run(record(connect, "wss://x", ["a"], 5, ParquetSink(tmp_path), 2, backoff_s=0))


def test_pinger_sends_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "PING_INTERVAL_S", 0.01)
    sock = FakeSocket([], False)

    async def run() -> None:
        task = asyncio.create_task(recorder._pinger(sock))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    assert "PING" in sock.sent
