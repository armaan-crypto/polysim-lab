import pytest

from pmsim.book.replay import TradePrint
from pmsim.engine.fills import Fill
from pmsim.strategies.base import (
    BookUpdate,
    Clock,
    GuardedSeries,
    LookaheadError,
    OrderIntent,
    Strategy,
    known_at,
)
from pmsim.strategies.legs import LegManager
from strategy_helpers import Market, S


def trades(clock: Clock) -> GuardedSeries[TradePrint]:
    items = [TradePrint("t", ts, ts, 5000, S) for ts in (100, 200, 300)]
    return GuardedSeries(clock, items, lambda tr: known_at(tr.ts, tr.recv_ts))


def test_clock_only_moves_forward() -> None:
    clock = Clock(10)
    clock.set(20)
    with pytest.raises(ValueError):
        clock.set(5)


def test_guarded_series_hides_future() -> None:
    clock = Clock(250)
    series = trades(clock)
    assert [t.ts for t in series] == [100, 200]
    assert series.latest() is not None and series.latest().ts == 200  # type: ignore[union-attr]
    assert series[1].ts == 200
    with pytest.raises(LookaheadError):
        series[2]
    clock.set(300)
    assert series[2].ts == 300


def test_known_at_uses_later_stamp() -> None:
    assert known_at(100, 105) == 105 and known_at(100, 98) == 100


def test_future_book_read_raises() -> None:
    m = Market(now=1000)
    m.binary("y", "n")
    m.book("y", [(4000, S)], [(4200, S)])
    m.book_times["y"] = 2000
    with pytest.raises(LookaheadError):
        m.view.book("y")


class Cheater(Strategy):
    """Peeks at the next trade in the full series."""

    def __init__(self, series: GuardedSeries[TradePrint]) -> None:
        self.series = series

    def on_book(self, update: BookUpdate) -> list:  # type: ignore[type-arg]
        nxt = self.series[2]
        return [OrderIntent("t", "BUY", nxt.price, S)]


def test_lookahead_guard_stops_cheating_strategy() -> None:
    clock = Clock(250)
    strat = Cheater(trades(clock))
    with pytest.raises(LookaheadError):
        strat.on_book(BookUpdate("t", 250))


def test_view_helpers() -> None:
    m = Market(now=0)
    m.binary("y", "n", end_ts=36 * 3_600_000)
    m.book("y", [(4000, 10 * S)], [(4200, 5 * S)])
    book = m.view.book("y")
    assert book is not None and book.mid() == 4100 and book.best_ask == (4200, 5 * S)
    assert m.view.book("missing") is None
    assert m.view.hours_to_end("y") == 36
    assert m.view.position("y") == 0 and m.view.open_orders("y") == []


def test_default_handlers_are_empty() -> None:
    class Idle(Strategy):
        pass

    s = Idle()
    fill = Fill(1, "t", "BUY", 1, 1, 0, "taker", 0, 0)
    assert s.on_trade(TradePrint("t", 0, 0, 1, 1)) == [] and s.on_timer(0) == []
    assert s.on_fill(fill, OrderIntent("t", "BUY", 1, 1)) == []


# ---- leg manager ----------------------------------------------------------------


def fill_for(intent: OrderIntent, size: int, price: int | None = None) -> Fill:
    px = intent.price if price is None else price
    return Fill(1, intent.token, intent.side, px, size, 0, "taker", 0, 0)


def test_legs_complete() -> None:
    lm = LegManager(timeout_ms=100)
    legs = lm.open(
        "x", [OrderIntent("a", "BUY", 4800, 10 * S), OrderIntent("b", "BUY", 4900, 10 * S)], 7, 0
    )
    assert legs[0].edge == 7 and legs[1].edge == 0 and legs[0].group == legs[1].group
    assert lm.busy(["a"])
    for leg in legs:
        lm.on_fill(fill_for(leg, 10 * S), leg)
    assert lm.stats.complete == 1 and not lm.busy(["a"])


def test_legs_unwind_on_imbalance() -> None:
    m = Market(now=0)
    m.binary("a", "b")
    m.book("a", [(4500, 100 * S)], [(4800, 100 * S)])
    lm = LegManager(timeout_ms=100, unwind_ticks=1)
    legs = lm.open(
        "x", [OrderIntent("a", "BUY", 4800, 10 * S), OrderIntent("b", "BUY", 4900, 10 * S)], 7, 0
    )
    lm.on_fill(fill_for(legs[0], 10 * S), legs[0])
    lm.on_fill(fill_for(legs[1], 4 * S), legs[1])
    assert lm.on_timer(m.view) == []  # before the deadline
    m.clock.set(100)
    out = lm.on_timer(m.view)
    assert out == [OrderIntent("a", "SELL", 4400, 6 * S, "IOC", legs[0].group, "unwind")]
    assert lm.stats.failed == 1
    unwind = out[0]
    assert isinstance(unwind, OrderIntent)
    lm.on_fill(fill_for(unwind, 6 * S), unwind)
    assert lm.unwind_cost() == 6 * S * (4800 - 4400)


def test_legs_no_bid_holds_excess() -> None:
    m = Market(now=0)
    m.binary("a", "b")
    lm = LegManager(timeout_ms=10)
    legs = lm.open(
        "x", [OrderIntent("a", "BUY", 4800, S * 5), OrderIntent("b", "BUY", 4900, S * 5)], 1, 0
    )
    lm.on_fill(fill_for(legs[0], 5 * S), legs[0])
    m.clock.set(10)
    assert lm.on_timer(m.view) == [] and lm.stats.failed == 1 and lm.unwind_cost() == 0


def test_legs_timeout_with_no_fills_is_not_failure() -> None:
    m = Market(now=0)
    lm = LegManager(timeout_ms=10)
    lm.open("x", [OrderIntent("a", "BUY", 1, S), OrderIntent("b", "BUY", 1, S)], 1, 0)
    m.clock.set(10)
    assert lm.on_timer(m.view) == [] and lm.stats.failed == 0
