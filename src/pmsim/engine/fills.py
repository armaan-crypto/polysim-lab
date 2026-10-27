"""Fill model: our simulated orders against a recorded L2 book and trade prints.

- Orders and cancels reach the exchange `latency_ms` after submission.
- A marketable order walks the visible book and pays real depth. Depth we
  take stays consumed until the recorded book shows it gone or a new
  snapshot arrives.
- A resting order fills only from recorded trades at or through its price,
  after the estimated queue ahead of it at that price is used up.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from pmsim.book.l2 import L2Book
from pmsim.book.lob import TimeInForce
from pmsim.book.replay import BookDelta, BookSnapshot, MarketEvent, TickChange, TradePrint
from pmsim.book.units import PRICE_SCALE, Side, crosses, opposite, sign
from pmsim.engine.fees import Liquidity, MarketRules

OrderStatus = Literal["pending", "resting", "filled", "cancelled", "rejected"]
OPEN: frozenset[OrderStatus] = frozenset({"pending", "resting"})


@dataclass(frozen=True)
class OrderRequest:
    token: str
    side: Side
    price: int
    size: int
    tif: TimeInForce = "GTC"


@dataclass
class SimOrder:
    id: int
    req: OrderRequest
    submit_ts: int
    remaining: int
    queue_ahead: int = 0
    status: OrderStatus = "pending"
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    order_id: int
    token: str
    side: Side
    price: int
    size: int
    ts: int
    liquidity: Liquidity
    fee: int
    gas: int

    @property
    def cash_delta(self) -> int:
        return -sign(self.side) * self.price * self.size - self.fee - self.gas

    @property
    def position_delta(self) -> int:
        return sign(self.side) * self.size


class FillEngine:
    """Simulated exchange for one run. Never talks to a real venue."""

    def __init__(
        self, rules: Mapping[str, MarketRules], latency_ms: int = 150, gas_cash: int = 0
    ) -> None:
        self.rules = dict(rules)
        self.latency_ms = latency_ms
        self.gas_cash = gas_cash
        self.books: dict[str, L2Book] = {}
        self.orders: dict[int, SimOrder] = {}
        self.now = 0
        self._consumed: dict[tuple[str, Side, int], int] = {}
        self._pending: list[tuple[int, int, str, int]] = []
        self._seq = 0
        self._open: dict[str, dict[int, SimOrder]] = {}  # token -> open orders by id

    # ---- requests -------------------------------------------------------

    def submit(self, req: OrderRequest, now: int) -> SimOrder:
        order = SimOrder(len(self.orders) + 1, req, now, req.size)
        self.orders[order.id] = order
        self._open.setdefault(req.token, {})[order.id] = order
        self._schedule(now + self.latency_ms, "activate", order.id)
        return order

    def cancel(self, order_id: int, now: int) -> bool:
        """Schedule a cancel. Returns False for an unknown order id."""
        if order_id not in self.orders:
            return False
        self._schedule(now + self.latency_ms, "cancel", order_id)
        return True

    def close_market(self, token: str, reason: str) -> None:
        """Exchange side: trading stopped, every open order on `token` is cancelled now."""
        for order in self.open_orders(token):
            self._close(order, "cancelled", reason)

    def open_orders(self, token: str | None = None) -> list[SimOrder]:
        """Live (pending or resting) orders, oldest first."""
        if token is not None:
            return list(self._open.get(token, {}).values())
        return sorted(
            (o for book in self._open.values() for o in book.values()), key=lambda o: o.id
        )

    # ---- time and market data ---------------------------------------------

    def advance(self, ts: int) -> list[Fill]:
        """Run every request that reaches the exchange at or before `ts`."""
        fills = self._run_pending(ts, inclusive=True)
        self.now = max(self.now, ts)
        return fills

    def on_event(self, ev: MarketEvent) -> list[Fill]:
        """Requests arriving strictly before the event run first."""
        fills = self._run_pending(ev.ts, inclusive=False)
        self.now = max(self.now, ev.ts)
        if isinstance(ev, BookSnapshot):
            self._on_snapshot(ev)
        elif isinstance(ev, BookDelta):
            self._on_delta(ev)
        elif isinstance(ev, TradePrint):
            fills.extend(self._on_trade(ev))
        elif isinstance(ev, TickChange):
            self.rules[ev.token] = replace(self.rules[ev.token], tick=ev.tick)
        return fills

    def visible(self, token: str, side: Side, price: int) -> int:
        """Recorded size at a level minus what we already took from it."""
        book = self.books.get(token)
        raw = book.size_at(side, price) if book else 0
        return max(0, raw - self._consumed.get((token, side, price), 0))

    # ---- internals ----------------------------------------------------------

    def _close(self, order: SimOrder, status: OrderStatus, reason: str = "") -> None:
        """Move an order to a final status and drop it from the open index."""
        order.status, order.reason = status, reason or order.reason
        self._open.get(order.req.token, {}).pop(order.id, None)

    def _schedule(self, at: int, action: str, order_id: int) -> None:
        self._seq += 1
        heapq.heappush(self._pending, (at, self._seq, action, order_id))

    def _run_pending(self, ts: int, inclusive: bool) -> list[Fill]:
        fills: list[Fill] = []
        while self._pending and (
            self._pending[0][0] < ts or (inclusive and self._pending[0][0] == ts)
        ):
            at, _, action, order_id = heapq.heappop(self._pending)
            self.now = max(self.now, at)
            order = self.orders[order_id]
            if action == "activate":
                fills.extend(self._activate(order, at))
            elif order.status in OPEN:
                self._close(order, "cancelled", "cancel request")
        return fills

    def _reject_reason(self, order: SimOrder) -> str:
        req = order.req
        rules = self.rules.get(req.token)
        if rules is None:
            return "unknown token"
        if req.size <= 0 or req.size < rules.min_size:
            return "below minimum size"
        if req.price % rules.tick or not rules.tick <= req.price <= PRICE_SCALE - rules.tick:
            return "price off tick or out of range"
        for other in self.open_orders(req.token):
            if other.status == "resting" and other.req.side != req.side:
                if crosses(req.side, req.price, other.req.price):
                    return "would cross own resting order"
        return ""

    def _activate(self, order: SimOrder, ts: int) -> list[Fill]:
        if order.status != "pending":
            return []
        if reason := self._reject_reason(order):
            self._close(order, "rejected", reason)
            return []
        req = order.req
        opp = opposite(req.side)
        book = self.books.get(req.token, L2Book())
        levels = [
            (p, self.visible(req.token, opp, p))
            for p, _ in book.levels(opp)
            if crosses(req.side, req.price, p)
        ]
        if req.tif == "FOK" and sum(v for _, v in levels) < order.remaining:
            self._close(order, "cancelled", "FOK not fully fillable")
            return []
        fills = []
        for price, avail in levels:
            qty = min(order.remaining, avail)
            if qty <= 0:
                continue
            key = (req.token, opp, price)
            self._consumed[key] = self._consumed.get(key, 0) + qty
            fills.append(self._fill(order, price, qty, ts, "taker"))
            if order.remaining == 0:
                break
        if order.remaining == 0:
            self._close(order, "filled")
        elif req.tif != "GTC":
            self._close(order, "cancelled", f"{req.tif} remainder")
        else:
            order.status = "resting"
            order.queue_ahead = self.visible(req.token, req.side, req.price)
        return fills

    def _fill(self, order: SimOrder, price: int, qty: int, ts: int, liq: Liquidity) -> Fill:
        order.remaining -= qty
        if order.remaining == 0:
            self._close(order, "filled")
        fee = self.rules[order.req.token].fees.fee(price, qty, liq)
        return Fill(
            order.id, order.req.token, order.req.side, price, qty, ts, liq, fee, self.gas_cash
        )

    def _on_snapshot(self, ev: BookSnapshot) -> None:
        self.books.setdefault(ev.token, L2Book()).apply_snapshot(ev.bids, ev.asks)
        for key in [k for k in self._consumed if k[0] == ev.token]:
            del self._consumed[key]
        self._clamp_queues(ev.token)

    def _on_delta(self, ev: BookDelta) -> None:
        book = self.books.setdefault(ev.token, L2Book())
        old = book.size_at(ev.side, ev.price)
        removed = book.apply_delta(ev.side, ev.price, ev.size)
        key = (ev.token, ev.side, ev.price)
        if key in self._consumed and ev.size < old:
            self._consumed[key] = max(0, self._consumed[key] - (old - ev.size))
        for price, _ in removed:
            self._consumed.pop((ev.token, opposite(ev.side), price), None)
        self._clamp_queues(ev.token)

    def _clamp_queues(self, token: str) -> None:
        for o in self.open_orders(token):
            if o.status == "resting":
                o.queue_ahead = min(o.queue_ahead, self.visible(token, o.req.side, o.req.price))

    def _on_trade(self, ev: TradePrint) -> list[Fill]:
        avail = ev.size
        resting = [o for o in self.open_orders(ev.token) if o.status == "resting"]
        resting.sort(key=lambda o: (-sign(o.req.side) * o.req.price, o.id))
        fills = []
        for o in resting:
            if avail <= 0:
                break
            if not crosses(o.req.side, o.req.price, ev.price):
                continue
            ahead = min(o.queue_ahead, avail)
            o.queue_ahead -= ahead
            avail -= ahead
            qty = min(o.remaining, avail)
            if qty > 0:
                avail -= qty
                fills.append(self._fill(o, o.req.price, qty, ev.ts, "maker"))
        return fills
