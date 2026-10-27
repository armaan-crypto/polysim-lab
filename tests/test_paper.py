import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from conftest import load_fixture, ws_frames
from pmsim.book.replay import BookDelta, BookSnapshot
from pmsim.book.units import CASH_SCALE, SIZE_SCALE
from pmsim.cli import main as cli
from pmsim.cli.main import main
from pmsim.data.catalog import build_catalog, save_catalog
from pmsim.data.models import Market
from pmsim.data.recorder import Socket, parse_message, record
from pmsim.engine import state
from pmsim.engine.backtest import Backtest
from pmsim.engine.fees import FeeModel, MarketRules
from pmsim.engine.fills import Fill
from pmsim.engine.paper import PaperSink, rows_to_events
from pmsim.engine.portfolio import Portfolio
from pmsim.engine.universe import MarketData, Universe
from pmsim.strategies.base import TokenMeta
from pmsim.strategies.market_making import MarketMaker

S = SIZE_SCALE


def test_rows_to_events_matches_frames() -> None:
    events = rows_to_events(parse_message(ws_frames()[0], 1_793_084_400_000))
    snaps = [e for e in events if isinstance(e, BookSnapshot)]
    assert snaps and all(e.recv_ts == 1_793_084_400_000 for e in snaps)
    deltas = rows_to_events(parse_message(ws_frames()[1], 5))
    assert all(isinstance(e, BookDelta) for e in deltas)
    assert rows_to_events({}) == []


# ---- state ------------------------------------------------------------------------


def test_state_round_trip(tmp_path: Path) -> None:
    pf = Portfolio(cash=100 * CASH_SCALE, now=10)
    pf.apply_fill(Fill(1, "t", "BUY", 5000, 10 * S, 20, "taker", 7, 0))
    pf.apply_fill(Fill(2, "t", "SELL", 6000, 4 * S, 30, "taker", 0, 0))
    pf.apply_fill(Fill(3, "u", "BUY", 1000, 5 * S, 40, "taker", 0, 0))
    pf.redeem("u", 0, 50)
    path = tmp_path / "s" / "p.json"
    state.save(pf, path)
    back = state.load(path)
    assert back is not None
    assert state.to_dict(back) == state.to_dict(pf)
    assert not path.with_suffix(".json.tmp").exists()


def test_state_missing_and_bad_version(tmp_path: Path) -> None:
    assert state.load(tmp_path / "nope.json") is None
    with pytest.raises(ValueError):
        state.from_dict({"version": 99})


# ---- paper sink ---------------------------------------------------------------------


def paper_bt(token: str, portfolio: Portfolio | None = None, start: int = 0) -> Backtest:
    meta = {token: TokenMeta(token, "m", 0, "", None)}
    rules = {token: MarketRules(10, 5 * S, FeeModel(0.0))}
    uni = Universe(meta, rules, "recorded")
    return Backtest(MarketMaker(), MarketData(uni, []), start_ts=start, portfolio=portfolio)


class WallClock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_paper_sink_trades_logs_and_saves(tmp_path: Path) -> None:
    frame = ws_frames()[0]
    token = json.loads(frame)[0]["asset_id"]
    start = int(json.loads(frame)[0]["timestamp"])
    pf = Portfolio(cash=1000 * CASH_SCALE, now=start)
    pf.apply_fill(Fill(1, token, "BUY", 600, 100 * S, start, "taker", 0, 0))  # inventory to offer
    bt = paper_bt(token, pf, start)
    clock = WallClock(start + 5_000)
    sink = PaperSink(bt, tmp_path / "log.jsonl", tmp_path / "state.json", 1.0, clock)
    sink.add(parse_message(frame, start + 5_000))
    lines = [json.loads(x) for x in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert lines and all(x["reason"] for x in lines)
    assert {x["kind"] for x in lines} == {"order"}
    assert bt.decisions == []  # written out and dropped from memory
    assert (tmp_path / "state.json").exists()  # first save is due after 1 s
    clock.t += 3_000
    sink.add({})  # heartbeat: timers still run
    sink.flush()
    assert state.load(tmp_path / "state.json") is not None


def test_paper_resume_keeps_portfolio(tmp_path: Path) -> None:
    pf = Portfolio(cash=123 * CASH_SCALE, now=5)
    state.save(pf, tmp_path / "s.json")
    restored = state.load(tmp_path / "s.json")
    bt = paper_bt("t", restored, start=10)
    assert bt.portfolio.cash == 123 * CASH_SCALE and bt.clock.now == 10


def test_paper_through_recorder_loop(tmp_path: Path) -> None:
    frames = ws_frames()[:5]
    token = json.loads(frames[0])[0]["asset_id"]
    start = int(json.loads(frames[0])[0]["timestamp"])

    class Sock:
        def __init__(self) -> None:
            self.left = list(frames)
            self.sent: list[str] = []

        async def send(self, m: str) -> None:
            self.sent.append(m)

        async def recv(self) -> str:
            if self.left:
                return self.left.pop(0)
            await asyncio.sleep(3600)
            raise AssertionError

    @asynccontextmanager
    async def connect(url: str) -> AsyncIterator[Socket]:
        yield Sock()

    bt = paper_bt(token, start=start)
    sink = PaperSink(bt, tmp_path / "l.jsonl", tmp_path / "s.json", 60, WallClock(start + 1))
    asyncio.run(record(connect, "wss://x", [token], 0.2, sink))
    assert (tmp_path / "s.json").exists()
    assert token in bt.engine.books


def test_paper_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(f"data_dir: {tmp_path / 'data'}\n")
    assert (
        main(
            [
                "--config",
                str(cfg_path),
                "paper",
                "--strategy",
                "market_making",
                "--markets",
                "x",
                "--hours",
                "0.001",
            ]
        )
        == 2
    )
    markets = [Market.model_validate(m) for m in load_fixture("gamma_markets.json")]
    save_catalog(build_catalog(markets), tmp_path / "data")
    calls: list[tuple[list[str], float]] = []

    async def fake_record(
        connect: Any, url: str, tokens: list[str], secs: float, sink: Any
    ) -> None:
        calls.append((tokens, secs))
        sink.flush()

    monkeypatch.setattr(cli, "record", fake_record)
    argv = [
        "--config",
        str(cfg_path),
        "paper",
        "--strategy",
        "market_making",
        "--markets",
        "xi-jinping",
        "--hours",
        "0.5",
        "--session",
        "t",
    ]
    assert main(argv) == 0
    assert len(calls[0][0]) == 2 and calls[0][1] == 1800
    assert "starting paper session" in capsys.readouterr().out
    assert main(argv) == 0
    assert "resuming paper session" in capsys.readouterr().out
    argv[6] = "nothing-matches"
    assert main(argv) == 2
