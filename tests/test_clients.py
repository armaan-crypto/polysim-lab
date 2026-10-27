from collections.abc import Callable
from typing import Any

import httpx
import pytest

from conftest import load_fixture
from pmsim.data.clob import ClobClient, DataApiClient
from pmsim.data.gamma import GammaClient
from pmsim.data.http import HttpClient

MakeHttp = Callable[[dict[str, Any]], tuple[HttpClient, list[httpx.Request]]]
TOKEN = load_fixture("clob_book.json")["asset_id"]


def test_gamma_parses_fixture_markets(make_http: MakeHttp) -> None:
    http, seen = make_http({"/markets": load_fixture("gamma_markets.json")})
    markets = GammaClient(http, "https://g").list_markets(limit=2)
    binary, negrisk = markets
    assert binary.outcomes == ["Yes", "No"]
    assert len(binary.token_ids) == 2 and TOKEN in binary.token_ids
    assert binary.tick_size == 0.001 and binary.min_order_size == 5
    assert binary.fee_schedule.taker_only is True
    assert binary.rules.startswith("This market will resolve")
    assert not binary.neg_risk and negrisk.neg_risk
    params = seen[0].url.params
    assert params["include_tag"] == "true" and params["active"] == "true"
    assert params["order"] == "volume24hr" and params["ascending"] == "false"


def test_gamma_paging_stops_on_short_page(make_http: MakeHttp) -> None:
    http, seen = make_http({"/markets": load_fixture("gamma_markets.json")})
    got = list(GammaClient(http, "https://g").iter_markets(250))
    assert len(got) == 2
    assert len(seen) == 1 and seen[0].url.params["limit"] == "100"


def test_gamma_by_slug_missing(make_http: MakeHttp) -> None:
    http, seen = make_http({"/markets": []})
    assert GammaClient(http, "https://g").market_by_slug("nope") is None
    assert seen[0].url.params["slug"] == "nope"


def test_gamma_tolerates_missing_optional_fields(make_http: MakeHttp) -> None:
    """Seen live: feeSchedule, resolutionSource and endDate absent, feeType null."""
    raw = load_fixture("gamma_markets.json")
    for key in ("feeSchedule", "resolutionSource", "endDate"):
        raw[0].pop(key)
    raw[0]["feeType"] = None
    http, _ = make_http({"/markets": raw})
    m = GammaClient(http, "https://g").list_markets()[0]
    assert m.fee_type is None and m.fee_schedule.rate == 0 and m.end_date is None


def test_gamma_skips_markets_without_tokens(make_http: MakeHttp) -> None:
    raw = load_fixture("gamma_markets.json")
    raw[0] = {**raw[0], "clobTokenIds": None}
    http, _ = make_http({"/markets": raw})
    assert len(GammaClient(http, "https://g").list_markets()) == 1


@pytest.fixture
def clob(make_http: MakeHttp) -> tuple[ClobClient, list[httpx.Request]]:
    http, seen = make_http(
        {
            "/book": load_fixture("clob_book.json"),
            "/price": load_fixture("clob_price.json"),
            "/midpoint": load_fixture("clob_midpoint.json"),
            "/last-trade-price": load_fixture("clob_last_trade_price.json"),
            "/prices-history": load_fixture("clob_prices_history.json"),
        }
    )
    return ClobClient(http, "https://c/"), seen


def test_clob_book(clob: tuple[ClobClient, list[httpx.Request]]) -> None:
    client, seen = clob
    book = client.book(TOKEN)
    assert book.asset_id == TOKEN and book.timestamp_ms > 1_700_000_000_000
    assert book.bids[-1].price < book.asks[-1].price  # best levels are last
    assert book.tick_size == 0.001 and book.neg_risk is False
    assert seen[0].url.params["token_id"] == TOKEN


def test_clob_prices(clob: tuple[ClobClient, list[httpx.Request]]) -> None:
    client, seen = clob
    assert client.price(TOKEN, "BUY") == 0.037
    assert client.midpoint(TOKEN) == 0.0375
    assert client.last_trade_price(TOKEN) == (0.042, "BUY")
    assert seen[0].url.params["side"] == "BUY"


def test_clob_history_interval_and_window(clob: tuple[ClobClient, list[httpx.Request]]) -> None:
    client, seen = clob
    pts = client.price_history(TOKEN)
    assert len(pts) > 10 and all(0 <= p.p <= 1 for p in pts)
    assert seen[-1].url.params["interval"] == "1d"
    client.price_history(TOKEN, start_ts=1, end_ts=2)
    params = seen[-1].url.params
    assert params["startTs"] == "1" and params["endTs"] == "2" and "interval" not in params


def test_data_api_trades(make_http: MakeHttp) -> None:
    http, seen = make_http({"/trades": load_fixture("data_trades.json")})
    trades = DataApiClient(http, "https://d").trades("0xabc", limit=3)
    assert len(trades) == 3
    assert {t.side for t in trades} <= {"BUY", "SELL"}
    assert all(0 < t.price < 1 and t.size > 0 for t in trades)
    assert seen[0].url.params["market"] == "0xabc"
