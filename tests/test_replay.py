import json
from pathlib import Path

import pytest

from conftest import ws_frames
from pmsim.book.replay import (
    BookDelta,
    BookSnapshot,
    TickChange,
    TradePrint,
    event_key,
    load_events,
    rebuild_books,
)
from pmsim.book.units import SIZE_SCALE
from pmsim.data.recorder import ParquetSink, parse_message
from pmsim.engine.fees import FeeModel, MarketRules
from pmsim.engine.fills import FillEngine, OrderRequest
from test_recorder import DOC_TICK, DOC_TRADE


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    sink = ParquetSink(tmp_path)
    for i, frame in enumerate(ws_frames()):
        sink.add(parse_message(frame, recv_ts_ms=1_793_084_400_000 + i))
    sink.add(parse_message(json.dumps(DOC_TRADE), 1_782_753_357_300))
    sink.add(parse_message(json.dumps(DOC_TICK), 1_782_753_357_300))
    sink.flush()
    return tmp_path


def test_events_sorted_and_typed(raw_dir: Path) -> None:
    events = load_events(raw_dir)
    assert events == sorted(events, key=event_key)
    kinds = {type(e) for e in events}
    assert kinds == {BookSnapshot, BookDelta, TradePrint, TickChange}


def test_snapshot_levels_best_first(raw_dir: Path) -> None:
    snap = next(e for e in load_events(raw_dir) if isinstance(e, BookSnapshot))
    bid_prices = [p for p, _ in snap.bids]
    assert bid_prices == sorted(bid_prices, reverse=True)


def test_token_filter(raw_dir: Path) -> None:
    events = load_events(raw_dir, tokens={DOC_TRADE["asset_id"]})
    assert {e.token for e in events} == {DOC_TRADE["asset_id"]}


def test_rebuilt_books_match_recorded_best_prices(raw_dir: Path) -> None:
    """Replaying snapshots plus deltas must end at the feed's own best bid/ask."""
    last_best: dict[str, tuple[float, float]] = {}
    for frame in ws_frames():
        msg = json.loads(frame)
        for ev in msg if isinstance(msg, list) else [msg]:
            for ch in ev.get("price_changes", []):
                last_best[ch["asset_id"]] = (float(ch["best_bid"]), float(ch["best_ask"]))
    books = rebuild_books(load_events(raw_dir))
    checked = 0
    for token, (bid, ask) in last_best.items():
        if token not in books:
            continue
        book = books[token]
        assert book.best("BUY") == round(bid * 10_000)
        assert book.best("SELL") == round(ask * 10_000)
        assert not book.is_crossed()
        checked += 1
    assert checked > 0


def test_missing_tables_ok(tmp_path: Path) -> None:
    assert load_events(tmp_path) == []


def test_engine_replays_recorded_book(raw_dir: Path) -> None:
    events = [e for e in load_events(raw_dir) if not isinstance(e, TickChange)]
    snap = next(e for e in events if isinstance(e, BookSnapshot))
    rules = {snap.token: MarketRules(tick=1, min_size=SIZE_SCALE, fees=FeeModel(0.05))}
    eng = FillEngine(rules)
    for ev in events:
        if ev.token == snap.token:
            eng.on_event(ev)
    ask, ask_size = eng.books[snap.token].levels("SELL")[0]
    order = eng.submit(OrderRequest(snap.token, "BUY", ask, ask_size + SIZE_SCALE, "IOC"), eng.now)
    fills = eng.advance(eng.now + 150)
    assert fills[0].price == ask and sum(f.size for f in fills) >= ask_size
    assert order.status in {"filled", "cancelled"}
