"""Live Polymarket market data through the public, read-only APIs.

Events come from Gamma `/events` (markets nested, as Polymarket shows them),
books and price history from the CLOB. Responses are cached in memory for a
few seconds so a busy dashboard does not hammer the APIs.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from pmsim.config import ApiConfig
from pmsim.data.clob import ClobClient, Interval
from pmsim.data.http import HttpClient
from pmsim.data.models import Market, OrderBookSnapshot, PricePoint

EVENTS_TTL_S = 30.0
BOOK_TTL_S = 2.0
HISTORY_TTL_S = 60.0
MARKET_TTL_S = 30.0
EVENT_PAGE = 100


@dataclass(frozen=True)
class LiveEvent:
    id: str
    slug: str
    title: str
    icon: str | None
    end: str | None
    volume: float
    volume_24h: float
    neg_risk: bool
    tags: tuple[str, ...]
    description: str
    markets: tuple[Market, ...]


@dataclass
class _Cache:
    ttl: float
    clock: Callable[[], float] = time.monotonic
    items: dict[Any, tuple[float, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: Any, load: Callable[[], Any]) -> Any:
        with self.lock:
            hit = self.items.get(key)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
        value = load()
        with self.lock:
            self.items[key] = (self.clock(), value)
        return value


def parse_event(raw: dict[str, Any]) -> LiveEvent | None:
    """An open event with at least one tradable market, else None."""
    markets = []
    for m in raw.get("markets") or []:
        if not m.get("clobTokenIds") or m.get("closed"):
            continue
        try:
            market = Market.model_validate(
                {**m, "events": [{"id": raw["id"], "title": raw.get("title", "")}]}
            )
        except ValidationError:
            continue
        markets.append(market)
    if not markets:
        return None
    tags = tuple(t.get("slug", "") for t in raw.get("tags") or [] if t.get("slug"))
    return LiveEvent(
        id=str(raw["id"]),
        slug=raw.get("slug", ""),
        title=raw.get("title", ""),
        icon=raw.get("icon") or raw.get("image"),
        end=raw.get("endDate"),
        volume=float(raw.get("volume") or 0),
        volume_24h=float(raw.get("volume24hr") or 0),
        neg_risk=bool(raw.get("negRisk")),
        tags=tags,
        description=raw.get("description") or "",
        markets=tuple(markets),
    )


class LiveData:
    """Cached, read-only access to live Polymarket data."""

    def __init__(self, api: ApiConfig, http: HttpClient | None = None) -> None:
        self.api = api
        self.http = http or HttpClient(api)
        self.clob = ClobClient(self.http, api.clob_url)
        self._events = _Cache(EVENTS_TTL_S)
        self._books = _Cache(BOOK_TTL_S)
        self._history = _Cache(HISTORY_TTL_S)
        self._markets = _Cache(MARKET_TTL_S)

    def events(self, pages: int = 2) -> list[LiveEvent]:
        """Open events by 24 h volume, biggest first."""
        return list(self._events.get(("events", pages), lambda: self._load_events(pages)))

    def event(self, slug: str) -> LiveEvent | None:
        for ev in self.events():
            if ev.slug == slug:
                return ev
        raw = self.http.get_json(f"{self.api.gamma_url}/events", {"slug": slug})
        return parse_event(raw[0]) if raw else None

    def book(self, token: str) -> OrderBookSnapshot:
        return self._books.get(token, lambda: self.clob.book(token))  # type: ignore[no-any-return]

    def history(self, token: str, interval: Interval) -> list[PricePoint]:
        fidelity = {"1h": 1, "6h": 5, "1d": 10, "1w": 60, "1m": 180, "max": 720}[interval]
        return list(
            self._history.get(
                (token, interval),
                lambda: self.clob.price_history(token, interval=interval, fidelity_min=fidelity),
            )
        )

    def market_status(self, slug: str) -> dict[str, Any] | None:
        """Gamma's raw record for one market, for resolution checks."""

        def load() -> dict[str, Any] | None:
            raw = self.http.get_json(f"{self.api.gamma_url}/markets", {"slug": slug})
            return raw[0] if raw else None

        return self._markets.get(slug, load)  # type: ignore[no-any-return]

    def _load_events(self, pages: int) -> list[LiveEvent]:
        out: list[LiveEvent] = []
        for page in range(pages):
            raw = self.http.get_json(
                f"{self.api.gamma_url}/events",
                {
                    "limit": EVENT_PAGE,
                    "offset": page * EVENT_PAGE,
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            out.extend(ev for ev in (parse_event(r) for r in raw) if ev is not None)
            if len(raw) < EVENT_PAGE:
                break
        return out


def resolved_payouts(raw: dict[str, Any] | None) -> list[float] | None:
    """Final payout per outcome if the market has resolved, else None."""
    if not raw or not raw.get("closed") or raw.get("umaResolutionStatus") != "resolved":
        return None
    prices = raw.get("outcomePrices")
    if isinstance(prices, str):
        prices = json.loads(prices)
    try:
        return [float(p) for p in prices or []]
    except (TypeError, ValueError):
        return None
