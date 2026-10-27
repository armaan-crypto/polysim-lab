"""Live market stream: Polymarket's displayed-price rule, book folding, and the
subscribe, resubscribe and reconnect cycle against a fake WebSocket."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from conftest import load_fixture, ws_frames
from pmsim.config import ApiConfig, Config
from pmsim.data.recorder import Connect, Socket
from pmsim.live import stream as stream_mod
from pmsim.live.data import LiveData
from pmsim.live.stream import LiveStream, displayed_price
from pmsim.ui import desk_api

FIRST = json.loads(ws_frames()[0])[0]  # a `book` snapshot from the recorded feed
TOKEN = FIRST["asset_id"]
LATER = int(FIRST["timestamp"]) + 5


def change(price: str, size: str, side: str = "BUY", asset: str = TOKEN) -> str:
    body = {"asset_id": asset, "price": price, "size": size, "side": side}
    return json.dumps(
        {"event_type": "price_change", "timestamp": str(LATER), "price_changes": [body]}
    )


class FakeSocket:
    def __init__(self, frames: list[str], drop: bool = False) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.drop = drop

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        if self.drop:
            raise ConnectionResetError("dropped")
        await asyncio.sleep(3600)
        raise AssertionError


def fake_connect(sockets: list[FakeSocket], used: list[FakeSocket]) -> Connect:
    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[Socket]:
        sock = sockets[min(len(used), len(sockets) - 1)]
        used.append(sock)
        yield sock

    return connect


def wait_for(check: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_mod, "RECV_TIMEOUT_S", 0.02)
    monkeypatch.setattr(stream_mod, "RESUBSCRIBE_S", 0.05)


def subscribed(tokens: list[str], frames: list[str]) -> tuple[LiveStream, list[FakeSocket]]:
    used: list[FakeSocket] = []
    live = LiveStream("wss://x", fake_connect([FakeSocket(frames)], used))
    live.watch(tokens)
    wait_for(lambda: all(live.top(t) is not None for t in tokens))
    return live, used


def test_displayed_price_follows_polymarket_rule() -> None:
    assert displayed_price(0.40, 0.42, 0.10) == pytest.approx(0.41)  # tight: midpoint
    assert displayed_price(0.03, 0.97, 0.20) == 0.20  # wide: last trade
    assert displayed_price(0.03, 0.97, None) == pytest.approx(0.5)  # wide, never traded
    assert displayed_price(None, 0.001, None) == pytest.approx(0.0005)  # no bids counts as 0
    assert displayed_price(None, None, 0.7) == 0.7


def test_apply_folds_snapshot_and_changes() -> None:
    live, used = subscribed([TOKEN], [ws_frames()[0]])
    assert json.loads(used[0].sent[0]) == {"assets_ids": [TOKEN], "type": "market"}
    assert live.top(TOKEN) == (0.063, 0.064)
    live.apply(change("0.0635", "10"))
    live.apply(change("0.064", "0", side="SELL"))
    live.apply(change("0.5", "1", asset="unknown"))
    live.apply("PONG")
    assert live.top(TOKEN) == (0.0635, 0.065)
    live.apply(
        json.dumps({"event_type": "tick_size_change", "asset_id": TOKEN, "new_tick_size": "0.0001"})
    )
    snap = live.snapshot(TOKEN)
    assert snap is not None and snap.tick_size == 0.0001 and snap.timestamp_ms == LATER
    assert {lv.price for lv in snap.asks} == {0.065, 0.066}
    live.close()


def test_books_are_untrusted_when_stale_or_unwatched() -> None:
    clock = [0.0]
    live = LiveStream("wss://x", fake_connect([FakeSocket([])], []), clock=lambda: clock[0])
    live.apply(json.dumps(FIRST))
    assert live.top(TOKEN) is None  # never subscribed
    live._subscribed = frozenset({TOKEN})
    assert live.top(TOKEN) == (0.063, 0.064)
    clock[0] = stream_mod.STALE_S + 1
    assert live.top(TOKEN) is None and live.snapshot(TOKEN) is None


def test_resubscribes_when_new_tokens_are_watched() -> None:
    other = json.loads(ws_frames()[0])[1]
    used: list[FakeSocket] = []
    first = FakeSocket([json.dumps(FIRST)])
    second = FakeSocket([json.dumps(FIRST), json.dumps(other)])
    live = LiveStream("wss://x", fake_connect([first, second], used))
    live.watch([TOKEN])
    wait_for(lambda: live.top(TOKEN) is not None)
    live.watch([other["asset_id"]])
    wait_for(lambda: live.top(other["asset_id"]) is not None)
    assert sorted(json.loads(used[-1].sent[0])["assets_ids"]) == sorted([TOKEN, other["asset_id"]])
    live.close()


def test_reconnects_after_a_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_mod, "BACKOFF_S", 0.0)
    used: list[FakeSocket] = []
    sockets = [FakeSocket([json.dumps(FIRST)], drop=True), FakeSocket([json.dumps(FIRST)])]
    live = LiveStream("wss://x", fake_connect(sockets, used))
    live.watch([TOKEN])
    wait_for(lambda: len(used) >= 2 and live.top(TOKEN) is not None)
    live.close()


def test_live_data_prefers_the_stream(make_http: Callable[..., Any]) -> None:
    book = load_fixture("clob_book.json")
    http, calls = make_http({"/book": book})
    live, _ = subscribed([TOKEN], [ws_frames()[0]])
    data = LiveData(ApiConfig(), http, stream=live)
    assert data.book(TOKEN).asks and not calls  # served from the stream
    rest = data.book(book["asset_id"])  # not streamed yet: REST fallback
    assert rest.asset_id == book["asset_id"] and len(calls) == 1
    live.close()


def test_close_without_start_is_safe() -> None:
    LiveStream("wss://x", fake_connect([FakeSocket([])], [])).close()


def test_quiet_socket_counts_as_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_mod, "STALE_S", 0.05)
    used: list[FakeSocket] = []
    live = LiveStream("wss://x", fake_connect([FakeSocket([])], used))
    live.watch([TOKEN])
    wait_for(lambda: len(used) >= 2)
    live.close()


def test_desk_api_serves_streamed_prices(make_http: Callable[..., Any], tmp_path: Path) -> None:
    events = load_fixture("gamma_events.json")
    fed = next(e for e in events if e["title"] == "Fed Decision in October?")
    yes = json.loads(fed["markets"][0]["clobTokenIds"])[0]
    book = {
        "event_type": "book",
        "asset_id": yes,
        "timestamp": "1",
        "bids": [{"price": "0.40", "size": "5"}],
        "asks": [{"price": "0.42", "size": "5"}],
    }
    http, _ = make_http({"/events": events})
    live, _ = subscribed([yes], [json.dumps(book)])
    ctx = desk_api.make_context(Config(data_dir=tmp_path), LiveData(ApiConfig(), http, stream=live))
    market = next(
        m for m in desk_api.live_event(ctx, fed["slug"])["markets"] if m["tokens"][0] == yes
    )
    assert market["prices"] == pytest.approx([0.41, 0.59])
    assert market["buy"] == pytest.approx([0.42, 0.60])
    live.close()
