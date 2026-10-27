"""Build a MarketView by hand for strategy tests."""

from collections.abc import Sequence

from pmsim.book.l2 import L2Book, Level
from pmsim.book.units import SIZE_SCALE, Side
from pmsim.engine.fees import FeeModel, MarketRules
from pmsim.risk.negrisk import NegRiskGroup
from pmsim.strategies.base import Clock, MarketView, TokenMeta

S = SIZE_SCALE


class FakeOrders:
    def __init__(self) -> None:
        self.orders: dict[str, list[tuple[int, Side, int, int]]] = {}

    def open_order_ids(self, token: str) -> list[tuple[int, Side, int, int]]:
        return self.orders.get(token, [])


class Market:
    """Mutable test market: set books, positions and time, then read `view`."""

    def __init__(self, now: int = 1_000_000, fee_rate: float = 0.0, tick: int = 100) -> None:
        self.clock = Clock(now)
        self.books: dict[str, L2Book] = {}
        self.book_times: dict[str, int] = {}
        self.meta: dict[str, TokenMeta] = {}
        self.rules: dict[str, MarketRules] = {}
        self.positions: dict[str, int] = {}
        self.cash = 10_000 * 10**10
        self.groups: list[NegRiskGroup] = []
        self.orders = FakeOrders()
        self.fee_rate = fee_rate
        self.tick = tick
        self.view = MarketView(
            self.clock,
            self.books,
            self.book_times,
            self.meta,
            self.rules,
            self.positions,
            lambda: self.cash,
            self.groups,
            0,
            self.orders,
        )

    def binary(
        self,
        yes: str,
        no: str,
        end_ts: int | None = None,
        rules_text: str = "",
        group_id: str = "",
        tags: Sequence[str] = (),
    ) -> None:
        for i, (tok, comp) in enumerate(((yes, no), (no, yes))):
            self.meta[tok] = TokenMeta(
                tok, f"m-{yes}", i, comp, end_ts, tuple(tags), rules_text, group_id
            )
            self.rules[tok] = MarketRules(self.tick, 5 * S, FeeModel(self.fee_rate))

    def book(self, token: str, bids: Sequence[Level] = (), asks: Sequence[Level] = ()) -> None:
        b = L2Book()
        b.apply_snapshot(bids, asks)
        self.books[token] = b
        self.book_times[token] = self.clock.now
