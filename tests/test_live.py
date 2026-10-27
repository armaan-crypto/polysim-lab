"""Live desk: data client, paper account, bots, background desk, desk API.

A fake Polymarket (httpx MockTransport) serves recorded event fixtures and a
book whose levels each test controls.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import load_fixture
from pmsim.book.units import CASH_SCALE, SIZE_SCALE
from pmsim.config import ApiConfig, Config
from pmsim.data.http import HttpClient
from pmsim.data.models import OrderBookSnapshot, PricePoint
from pmsim.data.synthetic import SyntheticConfig, generate
from pmsim.engine.backtest import Backtest
from pmsim.engine.fees import FeeModel
from pmsim.live.account import Instrument, PaperAccount, TradeError, walk
from pmsim.live.bots import BotManager, BotRule, RuleBot, RuleBotConfig, decide, history_data
from pmsim.live.data import LiveData, parse_event, resolved_payouts
from pmsim.live.worker import Desk
from pmsim.ui import desk_api
from pmsim.ui.api import ApiError

EVENTS = load_fixture("gamma_events.json")
FED = next(e for e in EVENTS if e["title"] == "Fed Decision in October?")
OPEN_EVENTS = sum(parse_event(e) is not None for e in EVENTS)
FED_MARKET = FED["markets"][0]
TOKEN = json.loads(FED_MARKET["clobTokenIds"])[0]


class FakePolymarket:
    def __init__(self) -> None:
        self.bids = [(0.40, 100.0), (0.39, 200.0)]
        self.asks = [(0.42, 50.0), (0.43, 100.0)]
        self.calls: list[str] = []
        self.resolved: dict[str, Any] | None = None
        self.fail_book = False

    def book(self) -> dict[str, Any]:
        return {
            "market": "m",
            "asset_id": TOKEN,
            "timestamp": "1793084264975",
            "hash": "h",
            "bids": [{"price": str(p), "size": str(s)} for p, s in sorted(self.bids)],
            "asks": [{"price": str(p), "size": str(s)} for p, s in sorted(self.asks, reverse=True)],
            "tick_size": "0.01",
            "last_trade_price": "0.41",
        }

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req.url.path)
        path = req.url.path
        if path == "/events":
            slug = req.url.params.get("slug")
            body = [e for e in EVENTS if e["slug"] == slug] if slug else EVENTS
            return httpx.Response(200, json=body)
        if path == "/book":
            return httpx.Response(500) if self.fail_book else httpx.Response(200, json=self.book())
        if path == "/prices-history":
            pts = [
                {"t": 1_700_000_000 + 3600 * i, "p": 0.5 + 0.05 * ((-1) ** i)} for i in range(48)
            ]
            return httpx.Response(200, json={"history": pts})
        if path == "/markets":
            return httpx.Response(200, json=[self.resolved] if self.resolved else [])
        return httpx.Response(404)


@pytest.fixture
def fake() -> FakePolymarket:
    return FakePolymarket()


@pytest.fixture
def data(fake: FakePolymarket) -> LiveData:
    api = ApiConfig(requests_per_second=1e9, backoff_s=0, max_retries=0)
    http = HttpClient(api, transport=httpx.MockTransport(fake.handler), sleep=lambda _: None)
    live = LiveData(api, http)
    live._books.ttl = 0  # always fresh in tests
    return live


@pytest.fixture
def ctx(tmp_path: Path, data: LiveData) -> desk_api.DeskContext:
    return desk_api.make_context(Config(data_dir=tmp_path), data)


def inst(fee: float = 0.0) -> Instrument:
    return Instrument(TOKEN, "Fed / No change", "slug-m", FED["slug"], "Yes", 0, fee, 0.01, 5)


def snapshot(fake: FakePolymarket) -> OrderBookSnapshot:
    return OrderBookSnapshot.model_validate(fake.book())


# ---- data --------------------------------------------------------------------------------


def test_parse_event_skips_closed_and_tokenless() -> None:
    ev = parse_event(FED)
    assert ev is not None and ev.title == "Fed Decision in October?" and ev.markets
    assert all(m.event_title == ev.title for m in ev.markets)
    closed = {**FED, "markets": [{**m, "closed": True} for m in FED["markets"]]}
    assert parse_event(closed) is None
    assert parse_event({**FED, "markets": [{"id": "x"}]}) is None


def test_live_data_caches_events(data: LiveData, fake: FakePolymarket) -> None:
    assert len(data.events()) == OPEN_EVENTS
    data.events()
    assert fake.calls.count("/events") == 1
    assert data.event(FED["slug"]) is not None
    assert data.book(TOKEN).asks
    assert len(data.history(TOKEN, "1w")) == 48


def test_resolved_payouts() -> None:
    assert resolved_payouts(None) is None
    assert resolved_payouts({"closed": False}) is None
    done = {"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": '["0", "1"]'}
    assert resolved_payouts(done) == [0.0, 1.0]
    assert resolved_payouts({**done, "umaResolutionStatus": "proposed"}) is None


# ---- account --------------------------------------------------------------------------------


def test_walk_budget_and_limit() -> None:
    levels = [(4200, 50 * SIZE_SCALE), (4300, 100 * SIZE_SCALE)]
    plan = walk(levels, FeeModel(0.0), "BUY", 30 * CASH_SCALE, None)
    assert plan[0] == (4200, 50 * SIZE_SCALE) and sum(q for _, q in plan) > 50 * SIZE_SCALE
    assert walk(levels, FeeModel(0.0), "BUY", None, 80 * SIZE_SCALE, limit=4200) == [
        (4200, 50 * SIZE_SCALE)
    ]


def test_market_buy_and_sell(tmp_path: Path, fake: FakePolymarket) -> None:
    acct = PaperAccount(tmp_path, 1000)
    row = acct.market_buy(inst(0.05), snapshot(fake), 30)
    assert row["shares"] > 50 and 0.42 < row["avg_price"] < 0.43 and row["fee"] > 0
    snap = acct.snapshot({TOKEN: 0.41})
    assert snap["cash"] == pytest.approx(970, abs=0.01)
    assert snap["positions"][0]["pnl"] < 0
    sold = acct.market_sell(inst(0.05), snapshot(fake), row["shares"])
    assert sold["avg_price"] <= 0.40
    assert acct.snapshot({})["positions"] == []
    assert [t["side"] for t in acct.trades()] == ["SELL", "BUY"]


def test_account_refuses_bad_orders(tmp_path: Path, fake: FakePolymarket) -> None:
    acct = PaperAccount(tmp_path, 100)
    with pytest.raises(TradeError, match="minimum"):
        acct.market_buy(inst(), snapshot(fake), 0.5)
    with pytest.raises(TradeError, match="not enough cash"):
        acct.market_buy(inst(), snapshot(fake), 500)
    with pytest.raises(TradeError, match="sell at most"):
        acct.market_sell(inst(), snapshot(fake), 1)
    with pytest.raises(TradeError, match="multiple"):
        acct.place_limit(inst(), snapshot(fake), "BUY", 0.405, 10)
    with pytest.raises(TradeError, match="minimum size"):
        acct.place_limit(inst(), snapshot(fake), "BUY", 0.40, 1)
    fake.asks = []
    with pytest.raises(TradeError, match="no sellers"):
        acct.market_buy(inst(), snapshot(fake), 5)


def test_limit_rests_then_fills_when_crossed(tmp_path: Path, fake: FakePolymarket) -> None:
    acct = PaperAccount(tmp_path, 1000)
    out = acct.place_limit(inst(), snapshot(fake), "BUY", 0.38, 100)
    assert out["filled"] is None and out["order"]["shares"] == 100
    assert acct.free_cash() == 1000 * CASH_SCALE - 3800 * 100 * SIZE_SCALE
    assert acct.match(TOKEN, snapshot(fake)) == []
    fake.asks = [(0.37, 60.0)]
    rows = acct.match(TOKEN, snapshot(fake))
    assert rows[0]["shares"] == 60 and rows[0]["avg_price"] == 0.38  # filled at our price
    assert acct.snapshot({})["orders"][0]["shares"] == 40
    acct.cancel(acct.snapshot({})["orders"][0]["id"])
    assert acct.snapshot({})["orders"] == []
    with pytest.raises(TradeError):
        acct.cancel(99)


def test_marketable_limit_fills_now(tmp_path: Path, fake: FakePolymarket) -> None:
    acct = PaperAccount(tmp_path, 1000)
    out = acct.place_limit(inst(), snapshot(fake), "BUY", 0.42, 80)
    assert out["filled"]["shares"] == 50 and out["order"]["shares"] == 30


def test_settle_and_persist(tmp_path: Path, fake: FakePolymarket) -> None:
    acct = PaperAccount(tmp_path, 1000)
    bought = acct.market_buy(inst(), snapshot(fake), 42)["shares"]
    again = PaperAccount(tmp_path, 1000)
    assert again.portfolio.size(TOKEN) == acct.portfolio.size(TOKEN)
    row = again.settle(TOKEN, 1.0)
    assert row is not None and row["kind"] == "resolved"
    assert again.snapshot({})["cash"] == pytest.approx(1000 - 42 + bought, abs=0.01)
    assert again.settle(TOKEN, 1.0) is None
    again.reset(500)
    assert again.snapshot({})["cash"] == 500 and again.trades() == []


# ---- bots -------------------------------------------------------------------------------------


def test_decide() -> None:
    rule = BotRule(entry="below", entry_price=0.4, take_profit=0.5, stop_loss=0.3, order_usd=10)
    assert decide(rule, 0.38, 0.39, 0).side == "BUY"  # type: ignore[union-attr]
    assert decide(rule, 0.42, 0.43, 0) is None
    assert decide(rule, 0.51, 0.52, 20).side == "SELL"  # type: ignore[union-attr]
    assert "stop loss" in decide(rule, 0.29, 0.30, 20).reason  # type: ignore[union-attr]
    capped = BotRule(entry_price=0.4, order_usd=50, max_position_usd=60)
    assert decide(capped, 0.38, 0.39, 100) is None
    up = BotRule(entry="above", entry_price=0.6)
    assert decide(up, 0.6, 0.61, 0) is not None and decide(up, 0.5, 0.51, 0) is None
    with pytest.raises(ValueError):
        BotRule(take_profit=0.3, stop_loss=0.4)


def test_rule_bot_backtests_on_synthetic_and_history() -> None:
    data = generate(SyntheticConfig(duration_hours=2, n_groups=0, n_nested=0))
    res = Backtest(
        RuleBot(RuleBotConfig(entry_price=0.6, take_profit=0.7, cooldown_s=0)), data
    ).run()
    assert res.fills
    pts = [PricePoint(t=1_700_000_000 + 600 * i, p=0.3 + 0.01 * (i % 20)) for i in range(200)]
    hist = history_data(pts, "y", "n", 0.01, "Test")
    assert hist.universe.label == "historical" and len(hist.events) == 400
    cfg = RuleBotConfig(entry_price=0.32, take_profit=0.45, tokens=["y"], cooldown_s=0)
    res = Backtest(RuleBot(cfg), hist).run()
    assert {f.side for f in res.fills} == {"BUY", "SELL"} and res.net_pnl > 0


def test_bot_manager_runs_live_bot(tmp_path: Path, data: LiveData, fake: FakePolymarket) -> None:
    clock = {"t": 1_000_000}
    acct = PaperAccount(tmp_path, 1000)
    mgr = BotManager(tmp_path, acct, data, clock_ms=lambda: clock["t"])
    rule = {
        "entry": "below",
        "entry_price": 0.45,
        "take_profit": 0.5,
        "order_usd": 20,
        "max_position_usd": 30,
        "cooldown_s": 60,
    }
    spec = mgr.create({"name": "dip", "instrument": inst().__dict__, "rule": rule, "interval_s": 5})
    mgr.tick()
    assert mgr.stats[spec.id].trades == 0  # not running yet
    mgr.set_running(spec.id, True)
    mgr.tick()
    st = mgr.stats[spec.id]
    assert st.trades == 1 and st.shares > 0 and acct.portfolio.size(TOKEN) > 0
    clock["t"] += 10_000
    mgr.tick()
    assert st.trades == 1  # cooldown
    clock["t"] += 120_000
    fake.bids = [(0.55, 500.0)]
    mgr.tick()
    assert st.trades == 2 and st.shares == pytest.approx(0) and st.realized > 0
    view = mgr.view(spec.id)[0]
    assert view["log"][0]["kind"] == "sell" and "take profit" in view["log"][0]["detail"]
    fake.fail_book = True
    clock["t"] += 120_000
    mgr.tick()
    assert mgr.view(spec.id)[0]["log"][0]["kind"] == "error"
    reloaded = BotManager(tmp_path, acct, data)
    assert reloaded.bots[spec.id].running and reloaded.stats[spec.id].trades == 2
    mgr.delete(spec.id)
    assert mgr.view() == []


def test_desk_matches_and_settles(tmp_path: Path, data: LiveData, fake: FakePolymarket) -> None:
    acct = PaperAccount(tmp_path, 1000)
    desk = Desk(acct, data, BotManager(tmp_path, acct, data), clock=lambda: 0.0)
    acct.place_limit(inst(), snapshot(fake), "BUY", 0.38, 10)
    fake.asks = [(0.37, 50.0)]
    desk.match_orders()
    assert acct.portfolio.size(TOKEN) == 10 * SIZE_SCALE
    fake.resolved = {"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": ["1", "0"]}
    data._markets.ttl = 0
    desk.step()
    assert acct.portfolio.size(TOKEN) == 0
    assert acct.trades()[0]["kind"] == "resolved"


# ---- desk api -------------------------------------------------------------------------------


def test_desk_api_market_data(ctx: desk_api.DeskContext) -> None:
    ev = desk_api.live_events(ctx, limit=10)
    assert ev["total"] == OPEN_EVENTS and ev["events"][0]["markets"][0]["tokens"]
    assert desk_api.live_events(ctx, "fed")["total"] == 1
    assert desk_api.live_event(ctx, FED["slug"])["description"]
    book = desk_api.live_book(ctx, TOKEN)
    assert (
        book["best_bid"] == 0.40
        and book["best_ask"] == 0.42
        and book["spread"] == pytest.approx(0.02)
    )
    assert len(desk_api.live_history(ctx, TOKEN, "1d")["points"]) == 48
    with pytest.raises(ApiError):
        desk_api.live_history(ctx, TOKEN, "2y")
    with pytest.raises(ApiError):
        desk_api.live_book(ctx, "")


def test_desk_api_trading_flow(ctx: desk_api.DeskContext) -> None:
    base = {"event_slug": FED["slug"], "token": TOKEN}
    q = desk_api.quote(ctx, {**base, "side": "BUY", "amount": 21})
    assert q["shares"] == 50 and q["avg_price"] == 0.42
    out = desk_api.trade(ctx, {**base, "side": "BUY", "type": "market", "amount": 21})
    assert out["filled"]["shares"] == 50 and out["portfolio"]["positions"]
    out = desk_api.trade(ctx, {**base, "side": "SELL", "type": "limit", "price": 0.5, "shares": 20})
    order_id = out["order"]["id"]
    assert desk_api.portfolio(ctx)["orders"][0]["id"] == order_id
    assert desk_api.remove_limit(ctx, {"id": order_id})["orders"] == []
    with pytest.raises(ApiError, match="side"):
        desk_api.trade(ctx, {**base, "side": "HOLD", "amount": 1})
    with pytest.raises(ApiError, match="not part"):
        desk_api.trade(ctx, {**base, "token": "nope", "side": "BUY", "amount": 5})
    with pytest.raises(ApiError, match="cash"):
        desk_api.trade(ctx, {**base, "side": "BUY", "amount": 10**9})
    assert desk_api.reset(ctx, {"cash": 2000})["cash"] == 2000
    with pytest.raises(ApiError):
        desk_api.reset(ctx, {"cash": 5})


def test_desk_api_bots(ctx: desk_api.DeskContext) -> None:
    base = {"event_slug": FED["slug"], "token": TOKEN}
    rule = {"entry": "below", "entry_price": 0.52, "take_profit": 0.58, "cooldown_s": 0}
    bt = desk_api.backtest_bot(ctx, {**base, "rule": rule, "interval": "1w"})
    assert bt["label"] == "historical" and "real prices" in bt["summary"] and bt["prices"]
    assert bt["trades_on_chart"]
    bot = desk_api.create_bot(ctx, {**base, "rule": rule, "name": "dip buyer"})
    assert bot["name"] == "dip buyer" and not bot["running"]
    assert desk_api.bot_action(ctx, bot["id"], "start")["running"]
    assert not desk_api.bot_action(ctx, bot["id"], "stop")["running"]
    assert desk_api.bots(ctx)["rule_fields"]["entry_price"]["title"] == "Entry price"
    with pytest.raises(ApiError):
        desk_api.bot_action(ctx, bot["id"], "explode")
    assert desk_api.bot_action(ctx, bot["id"], "delete")["bots"] == []
    with pytest.raises(ApiError):
        desk_api.bot_action(ctx, "missing", "start")
    with pytest.raises(ApiError, match="stop loss"):
        desk_api.create_bot(ctx, {**base, "rule": {"take_profit": 0.3, "stop_loss": 0.4}})
    with pytest.raises(ApiError, match="window"):
        desk_api.backtest_bot(ctx, {**base, "rule": rule, "interval": "6h"})


def test_desk_api_offline(ctx: desk_api.DeskContext, fake: FakePolymarket) -> None:
    fake.fail_book = True
    with pytest.raises(ApiError, match="could not reach Polymarket"):
        desk_api.live_book(ctx, TOKEN)
