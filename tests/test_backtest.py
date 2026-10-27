import json
from collections import Counter
from pathlib import Path

import polars as pl
import pytest

from conftest import load_fixture, ws_frames
from pmsim.book.replay import BookSnapshot, TradePrint
from pmsim.book.units import CASH_SCALE, PRICE_SCALE, SIZE_SCALE
from pmsim.config import ResolutionRisk
from pmsim.data.catalog import build_catalog, save_catalog
from pmsim.data.models import Market
from pmsim.data.recorder import ParquetSink, parse_message
from pmsim.data.synthetic import SyntheticConfig, generate
from pmsim.engine.backtest import Backtest, BacktestConfig
from pmsim.engine.experiment import (
    STRESSES,
    RecordedSpec,
    RunSpec,
    apply_stress,
    load_data,
    param_grid,
    run_full,
    run_many,
    stress_test,
    sweep,
    walk_forward,
)
from pmsim.engine.fees import FeeModel, MarketRules
from pmsim.engine.universe import MarketData, Universe, scale_depth
from pmsim.risk.resolution import Resolution
from pmsim.strategies.base import (
    BookUpdate,
    Intent,
    LookaheadError,
    OrderIntent,
    Strategy,
    TokenMeta,
)
from pmsim.strategies.registry import build_strategy

S = SIZE_SCALE
SMALL = SyntheticConfig(duration_hours=1, n_markets=2, n_groups=1)

# ---- synthetic data -------------------------------------------------------------


def test_synthetic_is_seeded() -> None:
    a, b = generate(SMALL), generate(SMALL)
    assert a.events == b.events
    assert generate(SMALL.model_copy(update={"seed": 7})).events != a.events
    assert a.universe.label == "synthetic"


def test_synthetic_defects_present() -> None:
    cfg = SyntheticConfig(
        duration_hours=2,
        p_halt=0.01,
        p_stale=0.02,
        p_flash_gap=0.02,
        p_mispricing=0.05,
        thin_fraction=0.5,
        n_markets=6,
    )
    data = generate(cfg)
    snaps = [e for e in data.events if isinstance(e, BookSnapshot)]
    assert any(not e.bids and not e.asks for e in snaps)  # halts
    yes_asks: dict[int, dict[str, int]] = {}
    for e in snaps:
        if e.asks:
            yes_asks.setdefault(e.ts, {})[e.token] = e.asks[0][0]
    pairs = [
        a[f"syn{m}-yes"] + a[f"syn{m}-no"]
        for a in yes_asks.values()
        for m in range(6)
        if f"syn{m}-yes" in a and f"syn{m}-no" in a
    ]
    assert min(pairs) < PRICE_SCALE  # injected mispricings
    depths = {e.token: e.bids[0][1] for e in snaps if e.bids}
    assert max(depths.values()) > 4 * min(depths.values())  # thin markets
    per_token = Counter(e.token for e in snaps)
    assert min(per_token.values()) < max(per_token.values())  # stale or halted gaps
    assert all(not b.is_crossed() for b in [_book(e) for e in snaps])


def _book(e: BookSnapshot):  # type: ignore[no-untyped-def]
    from pmsim.book.l2 import L2Book

    b = L2Book()
    b.apply_snapshot(e.bids, e.asks)
    return b


def test_synthetic_resolutions_cover_every_token() -> None:
    data = generate(SMALL)
    paid = {t for r in data.universe.resolutions for t in r.payouts}
    assert set(data.universe.meta) <= paid
    for r in data.universe.resolutions:
        poly = {t: p for t, p in r.payouts.items() if t in data.universe.meta}
        assert sum(poly.values()) in (PRICE_SCALE, len(poly) // 2 * PRICE_SCALE)


# ---- backtest loop ------------------------------------------------------------------


def tiny_data(resolve_yes: bool = True) -> MarketData:
    rules = MarketRules(100, 5 * S, FeeModel(0.0))
    meta = {
        "y": TokenMeta("y", "m", 0, "n", 10_000),
        "n": TokenMeta("n", "m", 1, "y", 10_000),
    }
    events = [
        BookSnapshot("y", 0, 0, ((4500, 100 * S),), ((4700, 100 * S),)),
        BookSnapshot("n", 0, 0, ((5100, 100 * S),), ((5200, 100 * S),)),
        TradePrint("y", 2000, 2000, 4500, 30 * S),
        BookSnapshot("y", 5000, 5000, ((4500, 100 * S),), ((4700, 100 * S),)),
    ]
    payouts = {"y": PRICE_SCALE if resolve_yes else 0, "n": 0 if resolve_yes else PRICE_SCALE}
    uni = Universe(
        meta,
        {"y": rules, "n": rules},
        "synthetic",
        resolutions=[Resolution(20_000, "winner", payouts)],
    )
    return MarketData(uni, events)


class Scripted(Strategy):
    name = "scripted"

    def __init__(self, intents: list[Intent]) -> None:
        self.todo = list(intents)
        self.seen: list[tuple[str, int]] = []

    def on_book(self, update: BookUpdate) -> list[Intent]:
        self.seen.append((update.token, update.ts))
        out, self.todo = self.todo, []
        return out


def test_same_time_events_batched() -> None:
    s = Scripted([])
    Backtest(s, tiny_data()).run()
    assert s.seen[:2] == [("y", 0), ("n", 0)]
    assert s.view.now >= 20_000


def test_complementary_pair_settles_at_resolution() -> None:
    s = Scripted(
        [OrderIntent("y", "BUY", 4700, 10 * S, "IOC"), OrderIntent("n", "BUY", 5200, 10 * S, "IOC")]
    )
    res = Backtest(s, tiny_data(), BacktestConfig(cash_usd=100)).run()
    assert len(res.fills) == 2
    # paid 0.99 per pair, got 1.00 back
    assert res.net_pnl == 10 * S * 100
    assert res.realized == res.net_pnl and res.resolutions == 1
    assert res.equity[-1][1] == res.final_equity


def test_resting_order_fills_from_trade_through_its_price() -> None:
    s = Scripted([OrderIntent("y", "BUY", 4600, 20 * S)])  # inside the spread, no queue
    res = Backtest(s, tiny_data(resolve_yes=False), BacktestConfig(cash_usd=100)).run()
    assert [(f.price, f.size, f.liquidity) for f in res.fills] == [(4600, 20 * S, "maker")]
    assert res.net_pnl == -20 * S * 4600  # YES resolved to 0


def test_resting_order_behind_queue_does_not_fill() -> None:
    s = Scripted([OrderIntent("y", "BUY", 4500, 20 * S)])  # 100 shares ahead, trade is 30
    res = Backtest(s, tiny_data(resolve_yes=False), BacktestConfig(cash_usd=100)).run()
    assert res.fills == [] and res.net_pnl == 0


def test_cash_and_share_reservation() -> None:
    s = Scripted(
        [
            OrderIntent("y", "BUY", 4000, 200 * S),  # $80 resting
            OrderIntent("n", "BUY", 5000, 100 * S),  # $50 more: over $100
            OrderIntent("n", "SELL", 5300, 10 * S),  # nothing held
        ]
    )
    res = Backtest(s, tiny_data(), BacktestConfig(cash_usd=100)).run()
    assert res.rejects == Counter({"insufficient cash": 1, "insufficient shares": 1})


def test_unknown_token_and_cancel_rejected() -> None:
    from pmsim.strategies.base import CancelIntent

    s = Scripted([OrderIntent("zz", "BUY", 4000, 10 * S), CancelIntent(99)])
    res = Backtest(s, tiny_data()).run()
    assert res.rejects == Counter({"unknown token": 1, "unknown order": 1})


def test_open_orders_cancelled_at_resolution() -> None:
    s = Scripted([OrderIntent("y", "BUY", 4000, 10 * S)])
    bt = Backtest(s, tiny_data())
    bt.run()
    assert all(o.status == "cancelled" for o in bt.engine.orders.values())


class Peeker(Strategy):
    name = "peeker"

    def on_book(self, update: BookUpdate) -> list[Intent]:
        self.view.trades["y"][0]  # the only trade is stamped 2000
        return []


def test_backtest_guards_trade_series() -> None:
    with pytest.raises(LookaheadError):
        Backtest(Peeker(), tiny_data()).run()


def test_backtest_deterministic() -> None:
    spec = RunSpec.make("complementary", {}, SMALL)
    a, b = run_full(spec), run_full(spec)
    assert a.net_pnl == b.net_pnl and a.fills == b.fills


def test_resolution_risk_perturbs_timing() -> None:
    risk = ResolutionRisk(p_late_proposal=0, p_dispute=1, p_reversal=1, p_fifty_fifty=0)
    s = Scripted([OrderIntent("y", "BUY", 4700, 10 * S, "IOC")])
    bt = Backtest(s, tiny_data(), BacktestConfig(cash_usd=100, resolution_risk=risk))
    res = bt.run()
    assert bt.resolutions[0].reversed and bt.resolutions[0].ts > 20_000
    assert res.net_pnl == -10 * S * 4700  # reversal: YES now pays 0


def test_external_venue_fills_cross_platform() -> None:
    res = run_full(RunSpec.make("cross_platform", {}, SMALL))
    assert any(f.token.startswith("kalshi:") for f in res.fills)
    assert res.label == "synthetic"


# ---- experiment runner ------------------------------------------------------------------


def test_param_grid() -> None:
    assert param_grid({"b": [1, 2], "a": ["x"]}) == [{"a": "x", "b": 1}, {"a": "x", "b": 2}]


def test_stresses_transform_inputs() -> None:
    data = generate(SMALL)
    cfg = BacktestConfig()
    assert apply_stress(data, cfg, "latency_x3")[1].latency_ms == 450
    half = apply_stress(data, cfg, "depth_x0.5")[0]
    snap = next(e for e in data.events if isinstance(e, BookSnapshot) and e.bids)
    snap2 = next(
        e
        for e in half.events
        if isinstance(e, BookSnapshot) and e.ts == snap.ts and e.token == snap.token
    )
    assert snap2.bids[0][1] == snap.bids[0][1] // 2 or snap2.bids[0][1] == int(
        snap.bids[0][1] * 0.5
    )
    fees = apply_stress(data, cfg, "fees_x2")[0]
    tok = next(iter(fees.universe.rules))
    assert fees.universe.rules[tok].fees.multiplier == 2.0
    with pytest.raises(ValueError):
        apply_stress(data, cfg, "nope")
    assert scale_depth([], 0.5) == []


def test_parallel_matches_serial() -> None:
    specs = [
        RunSpec.make("complementary", {"min_edge_usd_per_share": e}, SMALL) for e in (0.0, 0.01)
    ]
    assert run_many(specs, workers=2) == run_many(specs, workers=1)


def test_sweep_and_stress_report() -> None:
    results = sweep("complementary", {"min_edge_usd_per_share": [0.0, 0.5]}, SMALL)
    assert len(results) == 2 and results[1].fills == 0
    report = stress_test("complementary", {}, SMALL)
    assert [r.stress for r in report.results] == list(STRESSES)
    assert report.label == "synthetic"
    assert report.passed == all(r.net_pnl_usd > 0 for r in report.results)


def test_walk_forward_folds() -> None:
    folds = walk_forward("complementary", {"min_edge_usd_per_share": [0.0, 0.01]}, SMALL, folds=2)
    assert len(folds) == 2
    assert folds[0].test == (folds[1].train[0], folds[1].train[1])
    assert all(f.train[1] == f.test[0] for f in folds)


def test_unknown_strategy() -> None:
    with pytest.raises(KeyError):
        build_strategy("nope", {}, generate(SMALL).universe)


def test_recorded_data_backtest(tmp_path: Path) -> None:
    """Recorded fixture frames plus a catalog built from fixture markets."""
    raw = tmp_path / "raw"
    sink = ParquetSink(raw)
    for i, frame in enumerate(ws_frames()):
        sink.add(parse_message(frame, 1_793_084_400_000 + i))
    sink.flush()
    tokens = {r["asset_id"] for f in ws_frames() for r in _events(f)}
    markets = []
    for j, m in enumerate(load_fixture("gamma_markets.json")):
        ids = sorted(tokens)[2 * j : 2 * j + 2]
        markets.append(Market.model_validate({**m, "clobTokenIds": json.dumps(ids)}))
    save_catalog(build_catalog(markets), tmp_path)
    spec = RecordedSpec(str(tmp_path))
    data = load_data(spec)
    assert data.universe.label == "recorded" and data.events
    res = run_full(RunSpec.make("market_making", {}, spec))
    assert res.label == "recorded"
    assert pl.read_parquet(tmp_path / "catalog.parquet").height == 4


def _events(frame: str) -> list[dict[str, str]]:
    msg = json.loads(frame)
    items = msg if isinstance(msg, list) else [msg]
    out = []
    for ev in items:
        out.extend(ev.get("price_changes", [ev]))
    return out


def test_cash_scale_sanity() -> None:
    assert CASH_SCALE == PRICE_SCALE * SIZE_SCALE


def test_synthetic_nested_links_feed_logical_strategy() -> None:
    data = generate(SMALL)
    link = data.universe.logical_links[0]
    assert link.subset_no.endswith("sub-no") and link.superset_yes.endswith("sup-yes")
    strat = build_strategy("logical", {}, data.universe)
    assert strat.cfg.relations[0].subset_no == link.subset_no  # type: ignore[attr-defined]
    for r in data.universe.resolutions:
        if link.superset_yes in r.payouts:
            sub_yes = link.subset_no.replace("-no", "-yes")
            assert r.payouts[sub_yes] <= r.payouts[link.superset_yes]  # subset implies superset
