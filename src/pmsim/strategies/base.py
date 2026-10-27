"""Strategy interface, order intents, and the read-only market view with a lookahead guard.

A strategy only sees data known at the current sim time. Anything stamped
later raises `LookaheadError`.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pmsim.book.l2 import L2Book, Level
from pmsim.book.lob import TimeInForce
from pmsim.book.replay import TradePrint
from pmsim.book.units import Side
from pmsim.engine.fees import MarketRules
from pmsim.engine.fills import Fill
from pmsim.risk.negrisk import NegRiskGroup
from pmsim.risk.resolution import Resolution


class LookaheadError(RuntimeError):
    """A strategy tried to read data stamped after the current sim time."""


# ---- intents ---------------------------------------------------------------


@dataclass(frozen=True)
class OrderIntent:
    token: str
    side: Side
    price: int
    size: int
    tif: TimeInForce = "GTC"
    group: str = ""  # multi-leg group id, empty for single orders
    tag: str = ""
    edge: int = 0  # expected edge in cash units, set on the first leg of a group


@dataclass(frozen=True)
class CancelIntent:
    order_id: int


@dataclass(frozen=True)
class ExternalIntent:
    """An order on another venue (e.g. Kalshi). Filled by a mock venue in sims."""

    venue: str
    ticker: str
    side: Side
    outcome: str  # "yes" or "no"
    price: int
    size: int
    group: str = ""


Intent = OrderIntent | CancelIntent | ExternalIntent


# ---- time and guarded data ---------------------------------------------------


class Clock:
    """Sim time. Only the runner moves it."""

    def __init__(self, now: int = 0) -> None:
        self._now = now

    @property
    def now(self) -> int:
        return self._now

    def set(self, ts: int) -> None:
        if ts < self._now:
            raise ValueError(f"clock went back: {ts} < {self._now}")
        self._now = ts

    def check(self, stamp: int, what: str = "data") -> None:
        if stamp > self._now:
            raise LookaheadError(f"{what} stamped {stamp} read at {self._now}")


class GuardedSeries[T]:
    """A time series that hides items stamped after now and raises on direct access."""

    def __init__(self, clock: Clock, items: Sequence[T], stamp: Callable[[T], int]) -> None:
        self._clock = clock
        self._items = sorted(items, key=stamp)
        self._stamp = stamp

    def __getitem__(self, i: int) -> T:
        item = self._items[i]
        self._clock.check(self._stamp(item), "series item")
        return item

    def __iter__(self) -> Iterator[T]:
        for item in self._items:
            if self._stamp(item) > self._clock.now:
                return
            yield item

    def latest(self) -> T | None:
        past = list(self)
        return past[-1] if past else None


def known_at(ts: int, recv_ts: int) -> int:
    """When data became known locally: the later of exchange and receive time."""
    return max(ts, recv_ts)


# ---- market view ----------------------------------------------------------------


@dataclass(frozen=True)
class TokenMeta:
    token: str
    market_id: str
    outcome_index: int
    complement: str  # the other token of the same binary market
    end_ts: int | None
    tags: tuple[str, ...] = ()
    rules_text: str = ""
    group_id: str = ""


@dataclass(frozen=True)
class BookView:
    """Read-only copy of one token's book at the time it was taken."""

    as_of: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2


class OrdersSource(Protocol):
    def open_order_ids(self, token: str) -> list[tuple[int, Side, int, int]]: ...


@dataclass
class MarketView:
    """What a strategy may look at. The runner owns and updates the underlying state."""

    clock: Clock
    books: Mapping[str, L2Book]
    book_times: Mapping[str, int]
    meta: Mapping[str, TokenMeta]
    rules: Mapping[str, MarketRules]
    positions: Mapping[str, int]
    cash: Callable[[], int]
    groups: Sequence[NegRiskGroup] = ()
    gas_cash: int = 0
    orders: OrdersSource | None = None
    trades: Mapping[str, GuardedSeries[TradePrint]] = field(default_factory=dict)

    @property
    def now(self) -> int:
        return self.clock.now

    def book(self, token: str) -> BookView | None:
        book = self.books.get(token)
        if book is None:
            return None
        as_of = self.book_times.get(token, 0)
        self.clock.check(as_of, f"book {token}")
        return BookView(as_of, tuple(book.levels("BUY")), tuple(book.levels("SELL")))

    def position(self, token: str) -> int:
        return self.positions.get(token, 0)

    def open_orders(self, token: str) -> list[tuple[int, Side, int, int]]:
        """(order id, side, price, remaining) for our live orders on `token`."""
        return self.orders.open_order_ids(token) if self.orders else []

    def hours_to_end(self, token: str) -> float | None:
        end = self.meta[token].end_ts
        return None if end is None else (end - self.now) / 3_600_000


def end_ts_ms(end: datetime | None) -> int | None:
    return None if end is None else int(end.timestamp() * 1000)


# ---- strategy -------------------------------------------------------------------


@dataclass(frozen=True)
class BookUpdate:
    token: str
    ts: int


class Strategy(ABC):
    """Handlers return order intents. `view` is bound by the runner before any call."""

    name = "strategy"
    view: MarketView

    def bind(self, view: MarketView) -> None:
        self.view = view

    def on_book(self, update: BookUpdate) -> list[Intent]:
        return []

    def on_trade(self, trade: TradePrint) -> list[Intent]:
        return []

    def on_timer(self, t: int) -> list[Intent]:
        return []

    def on_resolution(self, event: Resolution) -> list[Intent]:
        return []

    def on_fill(self, fill: Fill, intent: OrderIntent) -> list[Intent]:
        return []

    def on_external_fill(self, intent: ExternalIntent, size: int, price: int) -> list[Intent]:
        """A fill on another venue, reported by the mock venue."""
        return []
