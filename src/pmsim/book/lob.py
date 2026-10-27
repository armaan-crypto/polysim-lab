"""Order-level (L3) limit order book with price-time priority.

Used for synthetic markets and as the reference matching model in tests.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from pmsim.book.units import Side, crosses

TimeInForce = Literal["GTC", "IOC", "FOK"]


@dataclass
class RestingOrder:
    id: int
    owner: str
    side: Side
    price: int
    size: int


@dataclass(frozen=True)
class Match:
    taker_id: int
    maker_id: int
    taker_owner: str
    maker_owner: str
    taker_side: Side
    price: int  # always the maker's price
    size: int


class LimitOrderBook:
    """FIFO within a price level; better prices match first."""

    def __init__(self) -> None:
        self._queues: dict[Side, dict[int, deque[RestingOrder]]] = {"BUY": {}, "SELL": {}}
        self._orders: dict[int, RestingOrder] = {}
        self._next_id = 1

    def submit(
        self, owner: str, side: Side, price: int, size: int, tif: TimeInForce = "GTC"
    ) -> tuple[int, list[Match]]:
        """Match against the opposite side, then rest any GTC remainder."""
        if size <= 0 or price <= 0:
            raise ValueError("price and size must be positive")
        order = RestingOrder(self._next_id, owner, side, price, size)
        self._next_id += 1
        if tif == "FOK" and self.fillable(side, price) < size:
            return order.id, []
        matches = self._match(order)
        if order.size > 0 and tif == "GTC":
            self._queues[side].setdefault(price, deque()).append(order)
            self._orders[order.id] = order
        return order.id, matches

    def cancel(self, order_id: int) -> bool:
        order = self._orders.pop(order_id, None)
        if order is None:
            return False
        queue = self._queues[order.side][order.price]
        queue.remove(order)
        if not queue:
            del self._queues[order.side][order.price]
        return True

    def best(self, side: Side) -> int | None:
        prices = self._queues[side]
        if not prices:
            return None
        return max(prices) if side == "BUY" else min(prices)

    def depth(self, side: Side, price: int) -> int:
        return sum(o.size for o in self._queues[side].get(price, ()))

    def fillable(self, side: Side, limit: int) -> int:
        """Opposite-side size an order on `side` at `limit` could take."""
        opp: Side = "SELL" if side == "BUY" else "BUY"
        return sum(
            o.size for p, q in self._queues[opp].items() if crosses(side, limit, p) for o in q
        )

    def orders(self) -> list[RestingOrder]:
        return list(self._orders.values())

    def _match(self, taker: RestingOrder) -> list[Match]:
        opp: Side = "SELL" if taker.side == "BUY" else "BUY"
        book = self._queues[opp]
        matches: list[Match] = []
        while taker.size > 0 and (best := self.best(opp)) is not None:
            if not crosses(taker.side, taker.price, best):
                break
            queue = book[best]
            maker = queue[0]
            qty = min(taker.size, maker.size)
            matches.append(
                Match(taker.id, maker.id, taker.owner, maker.owner, taker.side, best, qty)
            )
            taker.size -= qty
            maker.size -= qty
            if maker.size == 0:
                queue.popleft()
                del self._orders[maker.id]
                if not queue:
                    del book[best]
        return matches
