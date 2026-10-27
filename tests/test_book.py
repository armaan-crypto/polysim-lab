import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmsim.book.l2 import L2Book
from pmsim.book.lob import LimitOrderBook
from pmsim.book.units import (
    CASH_SCALE,
    cash_units,
    crosses,
    price_units,
    size_units,
    tick_units,
    to_usd,
    usd_to_cash,
)

# ---- units -----------------------------------------------------------------


def test_units_round_trip() -> None:
    assert price_units(0.0375) == 375
    assert size_units(2075.359918) == 2_075_359_918
    assert to_usd(cash_units(price_units(0.5), size_units(10))) == 5.0
    assert usd_to_cash(1.0) == CASH_SCALE
    assert usd_to_cash(0.0000004) == 0


def test_tick_units_rejects_sub_resolution_tick() -> None:
    assert tick_units(0.001) == 10
    with pytest.raises(ValueError):
        tick_units(0.00001)


def test_crosses() -> None:
    assert crosses("BUY", 50, 50) and crosses("BUY", 50, 49) and not crosses("BUY", 50, 51)
    assert crosses("SELL", 50, 50) and crosses("SELL", 50, 51) and not crosses("SELL", 50, 49)


# ---- L2 book ----------------------------------------------------------------


def test_l2_snapshot_and_delta() -> None:
    b = L2Book()
    b.apply_snapshot([(40, 10), (39, 5)], [(42, 7), (43, 1)])
    assert b.best("BUY") == 40 and b.best("SELL") == 42
    assert b.levels("BUY") == [(40, 10), (39, 5)]
    b.apply_delta("BUY", 40, 0)
    assert b.best("BUY") == 39
    b.apply_delta("SELL", 41, 3)
    assert b.levels("SELL")[0] == (41, 3)


def test_l2_newest_level_uncrosses() -> None:
    b = L2Book()
    b.apply_snapshot([(40, 10)], [(42, 7), (43, 1)])
    removed = b.apply_delta("BUY", 42, 2)
    assert removed == [(42, 7)] and b.best("SELL") == 43 and not b.is_crossed()


def test_l2_rejects_negative_size() -> None:
    with pytest.raises(ValueError):
        L2Book().apply_delta("BUY", 1, -1)


deltas = st.lists(
    st.tuples(st.sampled_from(["BUY", "SELL"]), st.integers(1, 99), st.integers(0, 1000)),
    max_size=80,
)


@given(deltas)
def test_l2_never_crossed_never_negative(ops: list[tuple[str, int, int]]) -> None:
    b = L2Book()
    for side, price, size in ops:
        b.apply_delta(side, price, size)
        assert not b.is_crossed()
        assert all(s > 0 for sd in ("BUY", "SELL") for _, s in b.levels(sd))


# ---- L3 order book ----------------------------------------------------------


def test_lob_price_time_priority() -> None:
    lob = LimitOrderBook()
    a, _ = lob.submit("a", "SELL", 50, 10)
    b, _ = lob.submit("b", "SELL", 50, 10)
    c, _ = lob.submit("c", "SELL", 49, 5)
    _, matches = lob.submit("t", "BUY", 50, 12)
    assert [(m.maker_id, m.price, m.size) for m in matches] == [(c, 49, 5), (a, 50, 7)]
    assert lob.depth("SELL", 50) == 13
    lob.cancel(a)
    _, matches = lob.submit("t", "BUY", 50, 1)
    assert matches[0].maker_id == b


def test_lob_ioc_and_fok() -> None:
    lob = LimitOrderBook()
    lob.submit("m", "SELL", 50, 5)
    _, none = lob.submit("t", "BUY", 50, 6, "FOK")
    assert none == [] and lob.depth("SELL", 50) == 5
    _, part = lob.submit("t", "BUY", 50, 6, "IOC")
    assert sum(m.size for m in part) == 5
    assert lob.best("BUY") is None  # IOC remainder does not rest


def test_lob_cancel_unknown() -> None:
    assert not LimitOrderBook().cancel(99)


def test_lob_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        LimitOrderBook().submit("x", "BUY", 0, 1)


lob_ops = st.lists(
    st.one_of(
        st.tuples(
            st.just("submit"),
            st.sampled_from(["a", "b", "c"]),
            st.sampled_from(["BUY", "SELL"]),
            st.integers(1, 20),
            st.integers(1, 50),
            st.sampled_from(["GTC", "IOC", "FOK"]),
        ),
        st.tuples(st.just("cancel"), st.integers(1, 60)),
    ),
    max_size=60,
)


@given(lob_ops)
def test_lob_invariants(ops: list[tuple[object, ...]]) -> None:
    """Never crosses, sizes stay positive, shares and cash are conserved."""
    lob = LimitOrderBook()
    shares: dict[str, int] = {}
    cash: dict[str, int] = {}
    submitted = 0
    for op in ops:
        if op[0] == "cancel":
            lob.cancel(int(op[1]))
            continue
        _, owner, side, price, size, tif = op
        submitted += size
        _, matches = lob.submit(owner, side, price, size, tif)
        assert sum(m.size for m in matches) <= size
        for m in matches:
            assert m.size > 0
            buyer, seller = (
                (m.taker_owner, m.maker_owner)
                if m.taker_side == "BUY"
                else (m.maker_owner, m.taker_owner)
            )
            shares[buyer] = shares.get(buyer, 0) + m.size
            shares[seller] = shares.get(seller, 0) - m.size
            cash[buyer] = cash.get(buyer, 0) - m.price * m.size
            cash[seller] = cash.get(seller, 0) + m.price * m.size
            # taker never trades worse than its limit
            assert crosses(m.taker_side, price, m.price)
        bid, ask = lob.best("BUY"), lob.best("SELL")
        assert bid is None or ask is None or bid < ask
        assert all(o.size > 0 for o in lob.orders())
    assert sum(shares.values()) == 0
    assert sum(cash.values()) == 0
