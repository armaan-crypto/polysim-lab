from datetime import UTC, datetime

import pytest

from conftest import load_fixture
from pmsim.book.units import PRICE_SCALE
from pmsim.data.kalshi import KalshiMarket, MockQuoteFeed, Quote, quote_from_orderbook
from pmsim.risk.negrisk import GroupMarket, NegRiskGroup
from pmsim.strategies.base import (
    BookUpdate,
    CancelIntent,
    ExternalIntent,
    LookaheadError,
    OrderIntent,
)
from pmsim.strategies.complementary import ComplementaryArb, ComplementaryConfig
from pmsim.strategies.cross_platform import CrossPair, CrossPlatformConfig, CrossPlatformSpread
from pmsim.strategies.logical import LogicalArb, LogicalConfig, Relation
from pmsim.strategies.market_making import MarketMaker, MarketMakingConfig
from pmsim.strategies.near_resolution import NearResolutionConfig, NearResolutionYield
from pmsim.strategies.negrisk_sum import NegRiskSumArb
from pmsim.strategies.rules_text import mismatch_score, resolution_terms, risk_score
from strategy_helpers import Market, S

HOUR = 3_600_000


def orders(intents: list) -> list[OrderIntent]:  # type: ignore[type-arg]
    return [i for i in intents if isinstance(i, OrderIntent)]


# ---- 1. complementary -------------------------------------------------------------


def comp_market(y_ask: int, n_ask: int, fee: float = 0.0) -> Market:
    m = Market(fee_rate=fee)
    m.binary("y", "n")
    m.book("y", [(y_ask - 200, 50 * S)], [(y_ask, 40 * S)])
    m.book("n", [(n_ask - 200, 50 * S)], [(n_ask, 30 * S)])
    return m


def test_complementary_buys_cheap_pair() -> None:
    m = comp_market(4800, 4900)
    s = ComplementaryArb()
    s.bind(m.view)
    out = orders(s.on_book(BookUpdate("y", m.clock.now)))
    assert [(o.token, o.side, o.price, o.size, o.tif) for o in out] == [
        ("y", "BUY", 4800, 30 * S, "IOC"),
        ("n", "BUY", 4900, 30 * S, "IOC"),
    ]
    assert out[0].edge == 300 * 30 * S  # $0.03 per share
    assert s.on_book(BookUpdate("n", m.clock.now)) == []  # group still open


def test_complementary_fees_kill_thin_edge() -> None:
    m = comp_market(4900, 5000, fee=0.07)  # 1% gross, ~3.5% fees
    s = ComplementaryArb()
    s.bind(m.view)
    assert s.on_book(BookUpdate("y", m.clock.now)) == []


def test_complementary_respects_position_limit() -> None:
    m = comp_market(4800, 4900)
    m.positions["y"] = 995 * S
    s = ComplementaryArb(ComplementaryConfig(max_position_shares=1000))
    s.bind(m.view)
    assert [o.size for o in orders(s.on_book(BookUpdate("y", m.clock.now)))] == [5 * S, 5 * S]
    m2 = comp_market(4800, 4900)
    m2.positions["y"] = 996 * S  # room below the 5 share minimum
    s2 = ComplementaryArb(ComplementaryConfig(max_position_shares=1000))
    s2.bind(m2.view)
    assert s2.on_book(BookUpdate("y", m2.clock.now)) == []


# ---- 2. neg-risk sum ----------------------------------------------------------------


def group_market(yes_asks: list[int], no_asks: list[int], other: bool = False) -> Market:
    m = Market()
    gm = []
    for i, (ya, na) in enumerate(zip(yes_asks, no_asks, strict=True)):
        m.binary(f"y{i}", f"n{i}", group_id="g")
        m.book(f"y{i}", [], [(ya, 20 * S)])
        m.book(f"n{i}", [], [(na, 20 * S)])
        gm.append(GroupMarket(f"m{i}", f"o{i}", f"y{i}", f"n{i}", other and i == 2))
    m.groups.append(NegRiskGroup("g", tuple(gm)))
    return m


def test_negrisk_buys_yes_set_below_one() -> None:
    m = group_market([3000, 3000, 3500], [7500, 7500, 7000])
    s = NegRiskSumArb()
    s.bind(m.view)
    out = orders(s.on_book(BookUpdate("y0", m.clock.now)))
    assert [o.token for o in out] == ["y0", "y1", "y2"]
    assert out[0].edge == 500 * 20 * S


def test_negrisk_buys_no_set_when_yes_sum_above_one() -> None:
    # YES sum 1.2; NO asks sum 1.9 for a set paying $2
    m = group_market([4000, 4000, 4000], [6300, 6300, 6400])
    s = NegRiskSumArb()
    s.bind(m.view)
    out = orders(s.on_book(BookUpdate("y0", m.clock.now)))
    assert [o.token for o in out] == ["n0", "n1", "n2"]
    assert out[0].edge == 1000 * 20 * S


def test_negrisk_skips_groups_with_other() -> None:
    m = group_market([3000, 3000, 3000], [7000, 7000, 7000], other=True)
    s = NegRiskSumArb()
    s.bind(m.view)
    assert s.on_book(BookUpdate("y0", m.clock.now)) == []


def test_negrisk_ignores_non_group_tokens() -> None:
    m = group_market([3000, 3000, 3500], [7500, 7500, 7000])
    m.binary("a", "b")
    s = NegRiskSumArb()
    s.bind(m.view)
    assert s.on_book(BookUpdate("a", m.clock.now)) == []


# ---- 3. near resolution ----------------------------------------------------------------


def nr_market(ask: int, hours: float, rules: str = "Resolves Yes if X happens.") -> Market:
    m = Market(now=0)
    m.binary("y", "n", end_ts=int(hours * HOUR), rules_text=rules)
    m.book("y", [(ask - 100, 100 * S)], [(ask, 100 * S)])
    return m


def test_near_resolution_buys_clean_high_price() -> None:
    m = nr_market(9700, 24)
    s = NearResolutionYield()
    s.bind(m.view)
    out = orders(s.on_book(BookUpdate("y", 0)))
    assert len(out) == 1 and out[0].price == 9700 and out[0].size == 100 * S
    assert out[0].edge == 300 * 100 * S


@pytest.mark.parametrize(
    ("ask", "hours", "rules"),
    [
        (9000, 24, "clean"),  # price below window
        (9700, 200, "clean"),  # too far from the end
        (9700, -1, "clean"),  # already ended
        (9700, 24, "If the game is postponed it resolves 50-50."),  # risky rules
        (9900, 72, "clean"),  # yield too low: ~1% over ~3 days is fine, so use low yield cap
    ],
)
def test_near_resolution_filters(ask: int, hours: float, rules: str) -> None:
    m = nr_market(ask, hours, rules)
    cfg = NearResolutionConfig(min_annualized_yield=1.5)
    s = NearResolutionYield(cfg)
    s.bind(m.view)
    assert s.on_book(BookUpdate("y", 0)) == []


# ---- 4. logical --------------------------------------------------------------------------


def logical_market(sub_no_ask: int, sup_yes_ask: int) -> tuple[Market, LogicalArb]:
    m = Market()
    m.binary("a_yes", "a_no")
    m.binary("b_yes", "b_no")
    m.book("a_no", [], [(sub_no_ask, 30 * S)])
    m.book("b_yes", [], [(sup_yes_ask, 30 * S)])
    rel = Relation(name="win_by_10_implies_win", subset_no="a_no", superset_yes="b_yes")
    s = LogicalArb(LogicalConfig(relations=[rel]))
    s.bind(m.view)
    return m, s


def test_logical_buys_when_subset_priced_above_superset() -> None:
    m, s = logical_market(4000, 5500)  # P(A) = 0.60 > P(B) = 0.55
    out = orders(s.on_book(BookUpdate("a_no", m.clock.now)))
    assert [o.token for o in out] == ["a_no", "b_yes"] and out[0].edge == 500 * 30 * S


def test_logical_no_trade_when_consistent() -> None:
    m, s = logical_market(5000, 5500)
    assert s.on_book(BookUpdate("b_yes", m.clock.now)) == []
    assert s.on_book(BookUpdate("other", m.clock.now)) == []


# ---- 5. market making -------------------------------------------------------------------


def mm_market(inv: int = 0) -> tuple[Market, MarketMaker]:
    m = Market()
    m.binary("y", "n")
    m.book("y", [(4800, 100 * S)], [(5200, 100 * S)])
    m.positions["y"] = inv
    s = MarketMaker(MarketMakingConfig(half_spread_ticks=1, quote_shares=50))
    s.bind(m.view)
    return m, s


def test_mm_flat_quotes_bid_only() -> None:
    m, s = mm_market()
    out = orders(s.on_book(BookUpdate("y", m.clock.now)))
    assert [(o.side, o.price, o.size) for o in out] == [("BUY", 4900, 50 * S)]


def test_mm_skews_against_inventory_and_offers_held_shares() -> None:
    m, s = mm_market(inv=300 * S)  # skew 3 ticks down
    q = s.quotes("y")
    assert q["BUY"] == (4600, 50 * S) and q["SELL"] == (4900, 50 * S)


def test_mm_inventory_cap_stops_bids() -> None:
    m, s = mm_market(inv=480 * S)
    assert "BUY" not in s.quotes("y")


def test_mm_post_only() -> None:
    m, s = mm_market()
    m.book("y", [(4900, S)], [(5000, S)])
    q = s.quotes("y")
    assert q["BUY"][0] <= 4900


def test_mm_requotes_moved_orders_once() -> None:
    m, s = mm_market()
    m.orders.orders["y"] = [(7, "BUY", 4500, 50 * S)]
    out = s.on_book(BookUpdate("y", m.clock.now))
    assert CancelIntent(7) in out and len(orders(out)) == 1
    again = s.on_book(BookUpdate("y", m.clock.now))
    assert CancelIntent(7) not in again


def test_mm_keeps_good_quote() -> None:
    m, s = mm_market()
    m.orders.orders["y"] = [(7, "BUY", 4900, 50 * S)]
    assert s.on_book(BookUpdate("y", m.clock.now)) == []


def test_mm_token_filter() -> None:
    m, _ = mm_market()
    s = MarketMaker(MarketMakingConfig(tokens=["other"]))
    s.bind(m.view)
    assert s.on_book(BookUpdate("y", m.clock.now)) == []


# ---- 6. cross platform -----------------------------------------------------------------


def cross(
    k_yes_bid: int, k_yes_ask: int, mismatch: float = 0.1, ts: int = 0
) -> tuple[Market, CrossPlatformSpread]:
    m = Market(now=1000)
    m.binary("py", "pn")
    m.book("py", [], [(4500, 40 * S)])
    m.book("pn", [], [(5700, 40 * S)])
    feed = MockQuoteFeed({"K": [Quote(ts, k_yes_bid, 25 * S, k_yes_ask, 25 * S)]})
    pair = CrossPair(name="p", poly_yes="py", poly_no="pn", kalshi_ticker="K", mismatch=mismatch)
    s = CrossPlatformSpread(feed, CrossPlatformConfig(pairs=[pair], kalshi_fee_rate=0.0))
    s.bind(m.view)
    return m, s


def test_cross_buys_poly_yes_and_kalshi_no() -> None:
    m, s = cross(5200, 5400)  # Kalshi NO ask = 0.48; 0.45 + 0.48 = 0.93
    out = s.on_book(BookUpdate("py", 1000))
    poly = orders(out)
    ext = [i for i in out if isinstance(i, ExternalIntent)]
    assert [(o.token, o.price, o.size) for o in poly] == [("py", 4500, 25 * S)]
    assert ext == [ExternalIntent("kalshi", "K", "BUY", "no", 4800, 25 * S, poly[0].group)]
    penalty = 500 * 25 * S // 10  # $0.05 * 0.1 per share
    assert poly[0].edge == 700 * 25 * S - penalty
    s.on_fill(_fill(poly[0]), poly[0])
    s.on_external_fill(ext[0], 25 * S, 4800)
    assert s.legs.stats.complete == 1


def test_cross_skips_high_mismatch_and_no_edge() -> None:
    m, s = cross(5200, 5400, mismatch=0.9)
    assert s.on_book(BookUpdate("py", 1000)) == []
    m, s = cross(4400, 4600)  # 0.45 + 0.56 > 1 and 0.57 + 0.46 > 1
    assert s.on_book(BookUpdate("py", 1000)) == []


def test_cross_future_quote_raises() -> None:
    class Leaky:
        def quote(self, ticker: str, now: int) -> Quote:
            return Quote(now + 1, 5200, S, 5400, S)

    m, s = cross(5200, 5400)
    s.feed = Leaky()
    with pytest.raises(LookaheadError):
        s.on_timer(1000)


def _fill(intent: OrderIntent):  # type: ignore[no-untyped-def]
    from pmsim.engine.fills import Fill

    return Fill(1, intent.token, intent.side, intent.price, intent.size, 0, "taker", 0, 0)


# ---- rules text ------------------------------------------------------------------------


def test_risk_score() -> None:
    assert risk_score("Resolves Yes if the Fed cuts.") == 0
    assert risk_score("If postponed, resolves 50-50 at the sole discretion of X") == 5.0


def test_mismatch_score() -> None:
    a = "Resolves Yes if CPI above 3.1% in October per https://www.bls.gov data."
    b = "Yes if the October CPI print exceeds 3.1% (source: bls.gov https://bls.gov)."
    end = datetime(2026, 11, 13, tzinfo=UTC)
    assert resolution_terms(a) == {"3.1%", "october", "bls.gov"}
    assert mismatch_score(a, b, end, end) == 0.0
    c = "Resolves Yes if unemployment above 4.5% in November per dol.gov."
    assert mismatch_score(a, c, end, None) == 1.0


# ---- kalshi data -------------------------------------------------------------------------


def test_kalshi_fixtures_consistent() -> None:
    market = KalshiMarket.model_validate(load_fixture("kalshi_market.json")["market"])
    q = quote_from_orderbook(load_fixture("kalshi_orderbook.json"), ts=5)
    assert q.yes_bid == round(market.yes_bid * PRICE_SCALE)
    assert q.yes_ask == round(market.yes_ask * PRICE_SCALE)  # YES ask = 1 - best NO bid
    assert q.no_ask == PRICE_SCALE - q.yes_bid and q.yes_ask_size > 0


def test_kalshi_empty_book_and_mock_feed() -> None:
    q = quote_from_orderbook({"orderbook_fp": {"yes_dollars": [], "no_dollars": None}}, 0)
    assert q.yes_bid == 0 and q.yes_ask == PRICE_SCALE
    feed = MockQuoteFeed({"K": [Quote(10, 1, 1, 2, 1), Quote(5, 3, 1, 4, 1)]})
    assert feed.quote("K", 4) is None
    assert feed.quote("K", 7) == Quote(5, 3, 1, 4, 1)
    assert feed.quote("K", 99) == Quote(10, 1, 1, 2, 1)
    with pytest.raises(ValueError):
        quote_from_orderbook({"orderbook_fp": None}, 0)
