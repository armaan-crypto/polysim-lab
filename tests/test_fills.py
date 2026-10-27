from datetime import date

import polars as pl
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pmsim.book.replay import BookDelta, BookSnapshot, TickChange, TradePrint
from pmsim.book.units import SIZE_SCALE, usd_to_cash
from pmsim.config import FeeConfig
from pmsim.engine.fees import FeeModel, MarketRules, fee_model, rules_from_catalog
from pmsim.engine.fills import FillEngine, OrderRequest

S = SIZE_SCALE  # one share
T = "tok"


def rules(rate: float = 0.0, tick: int = 100, min_size: int = 5 * S) -> dict[str, MarketRules]:
    return {T: MarketRules(tick=tick, min_size=min_size, fees=FeeModel(rate))}


def engine(rate: float = 0.0, latency: int = 150, gas: int = 0) -> FillEngine:
    eng = FillEngine(rules(rate), latency_ms=latency, gas_cash=gas)
    eng.on_event(
        BookSnapshot(
            T, 0, 0, bids=((4000, 100 * S), (3900, 50 * S)), asks=((4200, 30 * S), (4300, 40 * S))
        )
    )
    return eng


# ---- fees ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "price", "usd"),
    # values from the fee tables for 100 shares in the docs (checked 2026-10-27)
    [(0.07, 5000, 1.75), (0.07, 1000, 0.63), (0.05, 5000, 1.25), (0.04, 3000, 0.84)],
)
def test_fee_matches_doc_tables(rate: float, price: int, usd: float) -> None:
    fee = FeeModel(rate).fee(price, 100 * S, "taker")
    assert abs(fee - usd_to_cash(usd)) <= usd_to_cash(0.005)


def test_fee_symmetric_maker_free_and_multiplier() -> None:
    m = FeeModel(0.05)
    assert m.fee(3000, 100 * S, "taker") == m.fee(7000, 100 * S, "taker")
    assert m.fee(5000, 100 * S, "maker") == 0
    assert FeeModel(0.05, multiplier=2).fee(5000, S, "taker") == 2 * m.fee(5000, S, "taker")


def test_fee_model_falls_back_without_schedule() -> None:
    cfg = FeeConfig(default_taker_rate=0.05, checked_on=date(2026, 10, 27))
    assert fee_model(None, 0.0, cfg).taker_rate == 0.05
    assert fee_model("zero_fees", 0.0, cfg).taker_rate == 0.0


def test_rules_from_catalog() -> None:
    df = pl.DataFrame(
        {
            "token_id": ["a"],
            "tick_size": [0.001],
            "min_order_size": [5.0],
            "fee_type": ["crypto_fees_v2"],
            "fee_rate": [0.07],
        }
    )
    r = rules_from_catalog(df, FeeConfig())["a"]
    assert r.tick == 10 and r.min_size == 5 * S and r.fees.taker_rate == 0.07


# ---- marketable orders --------------------------------------------------------


def test_marketable_order_walks_book_after_latency() -> None:
    eng = engine(rate=0.05)
    order = eng.submit(OrderRequest(T, "BUY", 4300, 50 * S), now=1000)
    assert eng.advance(1149) == [] and order.status == "pending"
    fills = eng.advance(1150)
    assert [(f.price, f.size) for f in fills] == [(4200, 30 * S), (4300, 20 * S)]
    assert all(f.ts == 1150 and f.liquidity == "taker" and f.fee > 0 for f in fills)
    assert order.status == "filled"
    assert eng.visible(T, "SELL", 4300) == 20 * S


def test_consumed_depth_not_reused() -> None:
    eng = engine()
    eng.submit(OrderRequest(T, "BUY", 4200, 30 * S, "IOC"), 0)
    eng.advance(150)
    second = eng.submit(OrderRequest(T, "BUY", 4200, 10 * S, "IOC"), 200)
    assert eng.advance(350) == [] and second.status == "cancelled"


def test_recorded_decrease_releases_consumption() -> None:
    eng = engine()
    eng.submit(OrderRequest(T, "BUY", 4200, 10 * S, "IOC"), 0)
    eng.advance(150)
    assert eng.visible(T, "SELL", 4200) == 20 * S
    # someone else really took 10: the recorded level drops to 20
    eng.on_event(BookDelta(T, 200, 200, "SELL", 4200, 20 * S))
    assert eng.visible(T, "SELL", 4200) == 20 * S


def test_snapshot_resets_consumption() -> None:
    eng = engine()
    eng.submit(OrderRequest(T, "BUY", 4200, 30 * S, "IOC"), 0)
    eng.advance(150)
    eng.on_event(BookSnapshot(T, 300, 300, bids=(), asks=((4200, 30 * S),)))
    assert eng.visible(T, "SELL", 4200) == 30 * S


def test_fok_all_or_nothing() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "BUY", 4300, 71 * S, "FOK"), 0)
    assert eng.advance(150) == [] and o.status == "cancelled"
    o2 = eng.submit(OrderRequest(T, "BUY", 4300, 70 * S, "FOK"), 0)
    assert sum(f.size for f in eng.advance(150)) == 70 * S and o2.status == "filled"


def test_ioc_partial() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "SELL", 4000, 120 * S, "IOC"), 0)
    assert sum(f.size for f in eng.advance(150)) == 100 * S
    assert o.status == "cancelled" and o.remaining == 20 * S


# ---- validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("req", "reason"),
    [
        (OrderRequest(T, "BUY", 4050, 10 * S), "off tick"),
        (OrderRequest(T, "BUY", 0, 10 * S), "out of range"),
        (OrderRequest(T, "BUY", 10_000, 10 * S), "out of range"),
        (OrderRequest(T, "BUY", 4000, 4 * S), "minimum size"),
        (OrderRequest("nope", "BUY", 4000, 10 * S), "unknown token"),
    ],
)
def test_rejections(req: OrderRequest, reason: str) -> None:
    eng = engine()
    o = eng.submit(req, 0)
    eng.advance(150)
    assert o.status == "rejected" and reason in o.reason


def test_tick_change_applies_at_exchange_time() -> None:
    eng = engine()
    eng.on_event(TickChange(T, 10, 10, tick=10))
    o = eng.submit(OrderRequest(T, "BUY", 4010, 10 * S), 20)
    eng.advance(200)
    assert o.status == "resting"


def test_self_cross_rejected() -> None:
    eng = engine()
    eng.submit(OrderRequest(T, "BUY", 4100, 10 * S), 0)
    o = eng.submit(OrderRequest(T, "SELL", 4100, 10 * S), 10)
    eng.advance(200)
    assert o.status == "rejected" and "own" in o.reason


# ---- resting orders and queue ---------------------------------------------------


def test_resting_fills_only_after_queue_ahead() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "BUY", 4000, 20 * S), 0)
    eng.advance(150)
    assert o.status == "resting" and o.queue_ahead == 100 * S
    assert eng.on_event(TradePrint(T, 200, 200, 4000, 90 * S)) == []
    fills = eng.on_event(TradePrint(T, 300, 300, 4000, 25 * S))
    assert [(f.price, f.size, f.liquidity) for f in fills] == [(4000, 15 * S, "maker")]
    assert o.remaining == 5 * S and o.queue_ahead == 0


def test_trade_above_bid_does_not_fill() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "BUY", 4100, 10 * S), 0)
    eng.advance(150)
    assert o.queue_ahead == 0
    assert eng.on_event(TradePrint(T, 200, 200, 4200, 50 * S)) == []
    assert [f.size for f in eng.on_event(TradePrint(T, 300, 300, 4100, 50 * S))] == [10 * S]


def test_queue_shrinks_with_level() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "SELL", 4200, 10 * S), 0)
    eng.advance(150)
    assert o.queue_ahead == 30 * S
    eng.on_event(BookDelta(T, 200, 200, "SELL", 4200, 5 * S))
    assert o.queue_ahead == 5 * S


def test_trade_before_activation_ignored() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "BUY", 4100, 10 * S), 0)
    assert eng.on_event(TradePrint(T, 100, 100, 4100, 50 * S)) == []
    assert o.status == "pending" and o.remaining == 10 * S
    eng.advance(150)
    assert o.status == "resting"


def test_cancel_race() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "BUY", 4100, 10 * S), 0)
    eng.advance(150)
    eng.cancel(o.id, now=1000)  # reaches the exchange at 1150
    assert [f.size for f in eng.on_event(TradePrint(T, 1100, 1100, 4100, 4 * S))] == [4 * S]
    assert eng.on_event(TradePrint(T, 1200, 1200, 4100, 50 * S)) == []
    assert o.status == "cancelled" and o.remaining == 6 * S


def test_cancel_unknown_order() -> None:
    assert engine().cancel(99, 0) is False


def test_cancel_before_activation() -> None:
    eng = engine()
    o = eng.submit(OrderRequest(T, "BUY", 4300, 10 * S), 0)
    eng.cancel(o.id, now=0)
    assert eng.advance(1000) != []  # activation reaches the exchange first
    o2 = eng.submit(OrderRequest(T, "BUY", 4100, 10 * S), 2000)
    eng.cancel(o2.id, now=2000)
    eng.advance(3000)
    assert o2.status == "cancelled"


def test_gas_charged_per_fill() -> None:
    eng = engine(gas=usd_to_cash(0.01))
    eng.submit(OrderRequest(T, "BUY", 4300, 50 * S), 0)
    fills = eng.advance(150)
    assert all(f.gas == usd_to_cash(0.01) for f in fills) and len(fills) == 2


# ---- properties --------------------------------------------------------------------

events = st.lists(
    st.one_of(
        st.tuples(
            st.just("order"),
            st.sampled_from(["BUY", "SELL"]),
            st.integers(1, 99),
            st.integers(5, 200),
            st.sampled_from(["GTC", "IOC", "FOK"]),
        ),
        st.tuples(st.just("trade"), st.integers(1, 99), st.integers(1, 200)),
        st.tuples(
            st.just("delta"),
            st.sampled_from(["BUY", "SELL"]),
            st.integers(1, 99),
            st.integers(0, 200),
        ),
        st.tuples(st.just("cancel"), st.integers(1, 30)),
    ),
    max_size=50,
)


@settings(max_examples=200, deadline=None)
@given(events, st.sampled_from([0.0, 0.05, 0.07]))
def test_fill_engine_invariants(ops: list[tuple[object, ...]], rate: float) -> None:
    eng = FillEngine(rules(rate, tick=100, min_size=5 * S), latency_ms=150)
    eng.on_event(BookSnapshot(T, 0, 0, bids=((4000, 100 * S),), asks=((6000, 100 * S),)))
    ts = 0
    for op in ops:
        ts += 100
        if op[0] == "order":
            _, side, px, size, tif = op
            eng.submit(OrderRequest(T, side, int(px) * 100, int(size) * S, tif), ts)
            fills = eng.advance(ts)
        elif op[0] == "cancel":
            eng.cancel(int(op[1]), ts)
            fills = eng.advance(ts)
        elif op[0] == "trade":
            fills = eng.on_event(TradePrint(T, ts, ts, int(op[1]) * 100, int(op[2]) * S))
        else:
            _, side, px, size = op
            fills = eng.on_event(BookDelta(T, ts, ts, side, int(px) * 100, int(size) * S))
        book = eng.books[T]
        assert not book.is_crossed()
        for f in fills:
            assert f.size > 0 and f.fee >= 0
            # cash plus position value at the fill price is conserved, net of costs
            assert f.cash_delta + f.position_delta * f.price + f.fee + f.gas == 0
        for o in eng.orders.values():
            assert 0 <= o.remaining <= o.req.size and o.queue_ahead >= 0
            assert (o.status == "filled") == (o.remaining == 0)
        assert all(used >= 0 for used in eng._consumed.values())
