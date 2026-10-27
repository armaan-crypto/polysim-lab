import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pmsim.book.units import CASH_SCALE, PRICE_SCALE, SIZE_SCALE
from pmsim.engine.fills import Fill
from pmsim.engine.portfolio import MS_PER_YEAR, Portfolio, PortfolioError

S = SIZE_SCALE
USD = CASH_SCALE


def fill(side: str, price: int, size: int, ts: int, fee: int = 0, token: str = "y") -> Fill:
    return Fill(1, token, side, price, size, ts, "taker", fee, 0)  # type: ignore[arg-type]


def test_average_cost_and_pnl_with_fees() -> None:
    pf = Portfolio(cash=100 * USD)
    pf.apply_fill(fill("BUY", 4000, 10 * S, 0, fee=USD // 10))  # $4 + $0.10 fee
    pf.apply_fill(fill("BUY", 6000, 10 * S, 10))  # $6
    pos = pf.positions["y"]
    assert pos.cost == 10 * USD + USD // 10
    assert pf.cash == 90 * USD - USD // 10
    assert pos.unrealized(5000) == -USD // 10
    pf.apply_fill(fill("SELL", 7000, 5 * S, 20, fee=USD // 20))  # $3.50 - $0.05
    assert pos.size == 15 * S
    removed = (10 * USD + USD // 10) // 4
    assert pos.realized == 35 * USD // 10 - USD // 20 - removed
    assert pf.cash + pos.cost == 100 * USD + pf.realized()


def test_oversell_and_missing_cash_rejected() -> None:
    pf = Portfolio(cash=USD)
    with pytest.raises(PortfolioError):
        pf.apply_fill(fill("SELL", 5000, S, 0))
    with pytest.raises(PortfolioError):
        pf.apply_fill(fill("BUY", 5000, 3 * S, 0))


def test_time_cannot_go_back() -> None:
    pf = Portfolio(cash=USD)
    pf.advance(10)
    with pytest.raises(PortfolioError):
        pf.advance(5)


def test_redeem_winner_and_loser() -> None:
    pf = Portfolio(cash=10 * USD)
    pf.apply_fill(fill("BUY", 9500, 10 * S, 0, token="win"))
    pf.apply_fill(fill("BUY", 300, 10 * S, 0, token="lose"))
    pf.redeem("win", PRICE_SCALE, 100)
    pf.redeem("lose", 0, 100)
    assert pf.cash == 10 * USD - 98 * USD // 10 + 10 * USD
    assert pf.positions["win"].realized == USD // 2
    assert pf.positions["lose"].realized == -3 * USD // 10
    assert {c.token for c in pf.closed} == {"win", "lose"}


def test_refund_books_zero_pnl() -> None:
    pf = Portfolio(cash=10 * USD)
    pf.apply_fill(fill("BUY", 4000, 10 * S, 0, fee=123))
    pf.refund("y", 50)
    assert pf.cash == 10 * USD and pf.realized() == 0


def test_merge_pays_one_dollar_per_pair() -> None:
    pf = Portfolio(cash=10 * USD)
    pf.apply_fill(fill("BUY", 4500, 10 * S, 0, token="yes"))
    pf.apply_fill(fill("BUY", 5300, 10 * S, 0, token="no"))
    pf.merge("yes", "no", 10 * S, 10)
    assert pf.cash == 10 * USD + USD // 5
    assert pf.realized() == USD // 5
    with pytest.raises(PortfolioError):
        pf.merge("yes", "no", S, 20)


def test_convert_no_carries_basis() -> None:
    pf = Portfolio(cash=10 * USD)
    pf.apply_fill(fill("BUY", 7000, 3 * S, 0, token="no_a"))
    cash = pf.cash
    pf.convert_no("no_a", ["yes_b", "yes_c"], 3 * S, 10)
    assert pf.cash == cash and pf.size("no_a") == 0
    assert pf.size("yes_b") == pf.size("yes_c") == 3 * S
    assert pf.positions["yes_b"].cost + pf.positions["yes_c"].cost == 21 * USD // 10
    assert pf.realized() == 0
    with pytest.raises(PortfolioError):
        pf.convert_no("yes_b", [], S, 20)


def test_lockup_annualized_return_and_idle_cash() -> None:
    pf = Portfolio(cash=100 * USD)
    day = MS_PER_YEAR // 365
    pf.apply_fill(fill("BUY", 5000, 100 * S, 0))  # $50 locked, $50 idle
    pf.redeem("y", PRICE_SCALE, 10 * day)  # +$50 after 10 days
    closed = pf.closed[0]
    assert closed.holding_ms() == 10 * day
    assert closed.avg_capital() == 50 * USD
    assert closed.annualized_return() == pytest.approx(36.5)
    assert pf.idle_cash_fraction() == pytest.approx(0.5)


def test_reopened_position_gets_new_lot() -> None:
    pf = Portfolio(cash=10 * USD)
    pf.apply_fill(fill("BUY", 5000, S, 0))
    pf.apply_fill(fill("SELL", 6000, S, 10))
    pf.apply_fill(fill("BUY", 5000, S, 20))
    pf.apply_fill(fill("SELL", 4000, S, 30))
    assert [c.pnl for c in pf.closed] == [USD // 10, -USD // 10]
    assert [c.opened_ts for c in pf.closed] == [0, 20]


ops = st.lists(
    st.one_of(
        st.tuples(
            st.just("buy"),
            st.sampled_from(["a", "b"]),
            st.integers(1, 99),
            st.integers(1, 50),
            st.integers(0, 10**7),
        ),
        st.tuples(
            st.just("sell"),
            st.sampled_from(["a", "b"]),
            st.integers(1, 99),
            st.integers(1, 50),
            st.integers(0, 10**7),
        ),
        st.tuples(
            st.just("redeem"), st.sampled_from(["a", "b"]), st.sampled_from([0, 5000, 10000])
        ),
        st.tuples(st.just("merge"), st.integers(1, 20)),
        st.tuples(st.just("convert"), st.integers(1, 20)),
    ),
    max_size=40,
)


@settings(max_examples=300, deadline=None)
@given(ops)
def test_accounting_identity(seq: list[tuple[object, ...]]) -> None:
    pf = Portfolio(cash=500 * USD)
    marks = {"a": 3300, "b": 6700, "c": 1000}
    ts = 0
    for op in seq:
        ts += 1000
        try:
            if op[0] in ("buy", "sell"):
                _, tok, px, size, fee = op
                pf.apply_fill(
                    fill(
                        str(op[0]).upper(),
                        int(px) * 100,
                        int(size) * S,
                        ts,
                        fee=int(fee),
                        token=str(tok),
                    )
                )
            elif op[0] == "redeem":
                pf.redeem(str(op[1]), int(op[2]), ts)
            elif op[0] == "merge":
                pf.merge("a", "b", int(op[1]) * S, ts)
            else:
                pf.convert_no("a", ["b", "c"], int(op[1]) * S, ts)
        except PortfolioError:
            pass
        cost = sum(p.cost for p in pf.positions.values())
        assert pf.cash + cost == pf.initial_cash + pf.realized()
        assert pf.equity(marks) - pf.initial_cash == pf.realized() + pf.unrealized(marks)
        assert all(p.size >= 0 for p in pf.positions.values())
        assert all(p.size > 0 or p.cost == 0 for p in pf.positions.values())
        assert 0.0 <= pf.idle_cash_fraction() <= 1.0 or pf.cash < 0
