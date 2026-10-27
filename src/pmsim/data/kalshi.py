"""Kalshi: read-only market data client, a venue-neutral quote type, and a mock feed.

Kalshi books list bids only, for YES and NO. A YES ask at p is a NO bid at
1 - p (docs, getting_started/orderbook_responses, checked 2026-10-27).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.units import PRICE_SCALE, price_units, size_units
from pmsim.data.http import HttpClient

KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiMarket(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    ticker: str
    event_ticker: str
    title: str
    status: str
    close_time: str
    rules_primary: str = ""
    rules_secondary: str = ""
    yes_bid: float = Field(alias="yes_bid_dollars")
    yes_ask: float = Field(alias="yes_ask_dollars")


@dataclass(frozen=True)
class Quote:
    """Top of book for a binary market on any venue, in pmsim units."""

    ts: int
    yes_bid: int
    yes_bid_size: int
    yes_ask: int
    yes_ask_size: int

    @property
    def no_ask(self) -> int:
        return PRICE_SCALE - self.yes_bid

    @property
    def no_bid(self) -> int:
        return PRICE_SCALE - self.yes_ask


def quote_from_orderbook(raw: Mapping[str, object], ts: int) -> Quote:
    """Build a quote from a `/markets/{ticker}/orderbook` response."""
    book = raw["orderbook_fp"]
    if not isinstance(book, Mapping):
        raise ValueError("orderbook_fp missing")
    yes = _best(book.get("yes_dollars") or [])
    no = _best(book.get("no_dollars") or [])
    return Quote(ts, yes[0], yes[1], PRICE_SCALE - no[0], no[1])


def _best(levels: Sequence[Sequence[str]]) -> tuple[int, int]:
    if not levels:
        return 0, 0
    price, size = max(((price_units(float(p)), size_units(float(q))) for p, q in levels))
    return price, size


class KalshiClient:
    """Public market endpoints only. No authentication, no trading."""

    def __init__(self, http: HttpClient, base_url: str = KALSHI_URL) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def market(self, ticker: str) -> KalshiMarket:
        raw = self._http.get_json(f"{self._base}/markets/{ticker}")
        return KalshiMarket.model_validate(raw["market"])

    def quote(self, ticker: str, ts: int) -> Quote:
        return quote_from_orderbook(
            self._http.get_json(f"{self._base}/markets/{ticker}/orderbook"), ts
        )


class QuoteFeed(Protocol):
    def quote(self, ticker: str, now: int) -> Quote | None: ...


class MockQuoteFeed:
    """Scripted quotes for tests and synthetic runs. Returns the latest quote at or before `now`."""

    def __init__(self, quotes: Mapping[str, Sequence[Quote]]) -> None:
        self._quotes = {t: sorted(q, key=lambda x: x.ts) for t, q in quotes.items()}

    def quote(self, ticker: str, now: int) -> Quote | None:
        past = [q for q in self._quotes.get(ticker, ()) if q.ts <= now]
        return past[-1] if past else None
