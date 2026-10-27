"""Paper trading account that trades against live Polymarket order books.

Cash and positions are simulated and saved to disk. Orders never leave this
process: market orders walk the live book you pass in, and resting limit
orders fill when a later live book crosses their price (filled at the limit
price as maker). Taker fees follow the market's fee schedule,
fee = C * rate * p * (1 - p).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from pmsim.book.units import CASH_SCALE, PRICE_SCALE, SIZE_SCALE, price_units, size_units
from pmsim.data.models import OrderBookSnapshot
from pmsim.engine import state
from pmsim.engine.fees import FeeModel
from pmsim.engine.fills import Fill
from pmsim.engine.portfolio import Portfolio, PortfolioError

Side = Literal["BUY", "SELL"]
MIN_MARKET_USD = 1.0
Level = tuple[int, int]


class TradeError(ValueError):
    """An order the account or the book cannot fill; the message is shown to the user."""


@dataclass(frozen=True)
class Instrument:
    """What the account needs to know about one outcome token."""

    token: str
    label: str
    market_slug: str
    event_slug: str
    outcome: str
    outcome_index: int
    fee_rate: float
    tick: float
    min_size: float


@dataclass
class LimitOrder:
    id: int
    token: str
    side: Side
    price: int
    shares: int
    filled: int = 0
    created_ms: int = 0
    bot: str = ""

    @property
    def remaining(self) -> int:
        return self.shares - self.filled


@dataclass
class Execution:
    """Result of one order: what filled, at what average price, with what fee."""

    token: str
    side: Side
    shares: int = 0
    notional: int = 0  # cash units, before fees
    fee: int = 0
    fills: list[Fill] = field(default_factory=list)

    @property
    def avg_price(self) -> float:
        return self.notional / self.shares / PRICE_SCALE if self.shares else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "side": self.side,
            "shares": self.shares / SIZE_SCALE,
            "avg_price": self.avg_price,
            "notional": self.notional / CASH_SCALE,
            "fee": self.fee / CASH_SCALE,
        }


def book_levels(book: OrderBookSnapshot, side: Side) -> list[Level]:
    """Levels a taker on `side` trades against, best first."""
    rows = book.asks if side == "BUY" else book.bids
    levels = [(price_units(lv.price), size_units(lv.size)) for lv in rows if lv.size > 0]
    return sorted(levels, reverse=side == "SELL")


def _fee_model(inst: Instrument) -> FeeModel:
    return FeeModel(inst.fee_rate)


def walk(
    levels: list[Level],
    fees: FeeModel,
    side: Side,
    cash: int | None,
    shares: int | None,
    limit: int | None = None,
) -> list[Level]:
    """Plan taker fills: spend up to `cash` (buys) or sell up to `shares`, within `limit`."""
    plan: list[Level] = []
    left_cash, left_shares = cash, shares
    for price, size in levels:
        if limit is not None and (price > limit if side == "BUY" else price < limit):
            break
        q = size
        if left_shares is not None:
            q = min(q, left_shares)
        if left_cash is not None:
            per_share = price + fees.taker_rate * price * (PRICE_SCALE - price) / PRICE_SCALE
            q = min(q, int(left_cash // per_share))
            while q > 0 and price * q + fees.fee(price, q, "taker") > left_cash:
                q -= 1
        if q <= 0:
            break
        plan.append((price, q))
        if left_cash is not None:
            left_cash -= price * q + fees.fee(price, q, "taker")
        if left_shares is not None:
            left_shares -= q
            if left_shares == 0:
                break
    return plan


class PaperAccount:
    """Simulated cash and positions, saved under `root`. Thread-safe."""

    def __init__(
        self,
        root: Path,
        starting_cash_usd: float = 10_000.0,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.root = root
        self.clock_ms = clock_ms
        self.lock = threading.RLock()
        self.starting_cash_usd = starting_cash_usd
        self.instruments: dict[str, Instrument] = {}
        self.orders: dict[int, LimitOrder] = {}
        self.next_id = 1
        self.portfolio = Portfolio(cash=round(starting_cash_usd * CASH_SCALE), now=clock_ms())
        self._load()

    # ---- persistence ------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.root / "account.json"

    @property
    def _trades_path(self) -> Path:
        return self.root / "trades.jsonl"

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        raw = json.loads(self._state_path.read_text())
        self.portfolio = state.from_dict(raw["portfolio"])
        self.instruments = {t: Instrument(**i) for t, i in raw["instruments"].items()}
        self.orders = {o["id"]: LimitOrder(**o) for o in raw["orders"]}
        self.next_id = raw["next_id"]

    def save(self) -> None:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {
                "portfolio": state.to_dict(self.portfolio),
                "instruments": {t: asdict(i) for t, i in self.instruments.items()},
                "orders": [asdict(o) for o in self.orders.values()],
                "next_id": self.next_id,
            }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._state_path)

    def _record(self, kind: str, inst: Instrument, ex: Execution, bot: str = "") -> dict[str, Any]:
        row = {
            "ts": self.clock_ms(),
            "kind": kind,
            "label": inst.label,
            "outcome": inst.outcome,
            "event_slug": inst.event_slug,
            "bot": bot,
            **ex.as_dict(),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with self._trades_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def trades(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self._trades_path.exists():
            return []
        lines = self._trades_path.read_text().splitlines()[-limit:]
        return [json.loads(line) for line in reversed(lines)]

    def reset(self, cash_usd: float | None = None) -> None:
        with self.lock:
            cash = self.starting_cash_usd if cash_usd is None else cash_usd
            self.portfolio = Portfolio(cash=round(cash * CASH_SCALE), now=self.clock_ms())
            self.orders.clear()
            self.instruments.clear()
            self._trades_path.unlink(missing_ok=True)
            self.save()

    # ---- helpers -------------------------------------------------------------------

    def _now(self) -> int:
        return max(self.clock_ms(), self.portfolio.now)

    def _reserved_cash(self) -> int:
        return sum(o.price * o.remaining for o in self.orders.values() if o.side == "BUY")

    def free_cash(self) -> int:
        return self.portfolio.cash - self._reserved_cash()

    def free_shares(self, token: str) -> int:
        resting = sum(
            o.remaining for o in self.orders.values() if o.side == "SELL" and o.token == token
        )
        return self.portfolio.size(token) - resting

    def _apply(self, inst: Instrument, side: Side, plan: list[Level], liquidity: str) -> Execution:
        ex = Execution(inst.token, side)
        fees = _fee_model(inst)
        for price, q in plan:
            fee = fees.fee(price, q, "taker") if liquidity == "taker" else 0
            fill = Fill(0, inst.token, side, price, q, self._now(), liquidity, fee, 0)  # type: ignore[arg-type]
            self.portfolio.apply_fill(fill)
            ex.shares += q
            ex.notional += price * q
            ex.fee += fee
            ex.fills.append(fill)
        return ex

    def _check_tick(self, inst: Instrument, price: int) -> None:
        tick = price_units(inst.tick)
        if price % tick or not tick <= price <= PRICE_SCALE - tick:
            raise TradeError(f"price must be a multiple of {inst.tick:g} between 0 and 1")

    # ---- trading ---------------------------------------------------------------------

    def quote(
        self, inst: Instrument, book: OrderBookSnapshot, side: Side, amount: float
    ) -> dict[str, Any]:
        """Preview a market order without executing: dollars for buys, shares for sells."""
        fees = _fee_model(inst)
        levels = book_levels(book, side)
        if side == "BUY":
            plan = walk(levels, fees, side, round(amount * CASH_SCALE), None)
        else:
            plan = walk(levels, fees, side, None, size_units(amount))
        shares = sum(q for _, q in plan)
        notional = sum(p * q for p, q in plan)
        fee = sum(fees.fee(p, q, "taker") for p, q in plan)
        return {
            "shares": shares / SIZE_SCALE,
            "avg_price": notional / shares / PRICE_SCALE if shares else None,
            "cost": (notional + fee) / CASH_SCALE if side == "BUY" else None,
            "proceeds": (notional - fee) / CASH_SCALE if side == "SELL" else None,
            "fee": fee / CASH_SCALE,
            "payout_if_win": shares / SIZE_SCALE if side == "BUY" else None,
        }

    def market_buy(
        self, inst: Instrument, book: OrderBookSnapshot, amount_usd: float, bot: str = ""
    ) -> dict[str, Any]:
        if amount_usd < MIN_MARKET_USD:
            raise TradeError(f"minimum order is ${MIN_MARKET_USD:.0f}")
        with self.lock:
            budget = round(amount_usd * CASH_SCALE)
            if budget > self.free_cash():
                raise TradeError(
                    f"not enough cash: ${self.free_cash() / CASH_SCALE:,.2f} available"
                )
            plan = walk(book_levels(book, "BUY"), _fee_model(inst), "BUY", budget, None)
            if not plan:
                raise TradeError("no sellers in the book at the moment")
            self.instruments[inst.token] = inst
            ex = self._apply(inst, "BUY", plan, "taker")
            row = self._record("market", inst, ex, bot)
            self.save()
            return row

    def market_sell(
        self, inst: Instrument, book: OrderBookSnapshot, shares: float, bot: str = ""
    ) -> dict[str, Any]:
        with self.lock:
            want = size_units(shares)
            if want <= 0:
                raise TradeError("enter a number of shares")
            if want > self.free_shares(inst.token):
                held = self.free_shares(inst.token) / SIZE_SCALE
                raise TradeError(f"you can sell at most {held:,.2f} shares")
            plan = walk(book_levels(book, "SELL"), _fee_model(inst), "SELL", None, want)
            if not plan:
                raise TradeError("no buyers in the book at the moment")
            self.instruments[inst.token] = inst
            ex = self._apply(inst, "SELL", plan, "taker")
            row = self._record("market", inst, ex, bot)
            self.save()
            return row

    def place_limit(
        self,
        inst: Instrument,
        book: OrderBookSnapshot,
        side: Side,
        price: float,
        shares: float,
        bot: str = "",
    ) -> dict[str, Any]:
        """Marketable part fills now as taker; the rest rests until the book crosses it."""
        with self.lock:
            px, q = price_units(price), size_units(shares)
            self._check_tick(inst, px)
            if shares < inst.min_size:
                raise TradeError(f"minimum size is {inst.min_size:g} shares")
            if side == "BUY" and px * q > self.free_cash():
                raise TradeError(
                    f"not enough cash: ${self.free_cash() / CASH_SCALE:,.2f} available"
                )
            if side == "SELL" and q > self.free_shares(inst.token):
                raise TradeError(
                    f"you can sell at most {self.free_shares(inst.token) / SIZE_SCALE:,.2f} shares"
                )
            self.instruments[inst.token] = inst
            budget = self.free_cash() if side == "BUY" else None
            plan = walk(book_levels(book, side), _fee_model(inst), side, budget, q, limit=px)
            filled = sum(n for _, n in plan)
            result: dict[str, Any] = {"filled": None, "order": None}
            if plan:
                ex = self._apply(inst, side, plan, "taker")
                result["filled"] = self._record("limit", inst, ex, bot)
            if q - filled > 0:
                order = LimitOrder(
                    self.next_id, inst.token, side, px, q - filled, 0, self._now(), bot
                )
                self.next_id += 1
                self.orders[order.id] = order
                result["order"] = self._order_dict(order)
            self.save()
            return result

    def cancel(self, order_id: int) -> None:
        with self.lock:
            if self.orders.pop(order_id, None) is None:
                raise TradeError("order not found")
            self.save()

    def match(self, token: str, book: OrderBookSnapshot) -> list[dict[str, Any]]:
        """Fill resting orders on `token` that the live book now crosses."""
        rows = []
        with self.lock:
            for order in [o for o in self.orders.values() if o.token == token]:
                levels = book_levels(book, order.side)
                crossing = sum(
                    q
                    for p, q in levels
                    if (p <= order.price if order.side == "BUY" else p >= order.price)
                )
                q = min(order.remaining, crossing)
                if order.side == "SELL":
                    q = min(q, self.portfolio.size(token))
                if q <= 0:
                    continue
                inst = self.instruments[token]
                try:
                    ex = self._apply(inst, order.side, [(order.price, q)], "maker")
                except PortfolioError:
                    continue
                order.filled += q
                rows.append(self._record("limit fill", inst, ex, order.bot))
                if order.remaining <= 0:
                    del self.orders[order.id]
            if rows:
                self.save()
        return rows

    def settle(self, token: str, payout: float) -> dict[str, Any] | None:
        """Redeem a resolved position at `payout` dollars per share."""
        with self.lock:
            for oid in [o.id for o in self.orders.values() if o.token == token]:
                del self.orders[oid]
            size = self.portfolio.size(token)
            inst = self.instruments.get(token)
            if not size or inst is None:
                self.save()
                return None
            px = price_units(payout)
            self.portfolio.redeem(token, px, self._now())
            ex = Execution(token, "SELL", size, px * size)
            row = self._record("resolved", inst, ex)
            self.save()
            return row

    # ---- views ------------------------------------------------------------------------

    def _order_dict(self, o: LimitOrder) -> dict[str, Any]:
        inst = self.instruments.get(o.token)
        return {
            "id": o.id,
            "token": o.token,
            "label": inst.label if inst else o.token,
            "outcome": inst.outcome if inst else "",
            "event_slug": inst.event_slug if inst else "",
            "side": o.side,
            "price": o.price / PRICE_SCALE,
            "shares": o.remaining / SIZE_SCALE,
            "bot": o.bot,
        }

    def open_tokens(self) -> list[str]:
        held = [t for t, p in self.portfolio.positions.items() if p.size]
        return sorted(set(held) | {o.token for o in self.orders.values()})

    def snapshot(self, marks: dict[str, float]) -> dict[str, Any]:
        """Cash, positions valued at `marks` (dollars per share), open orders."""
        with self.lock:
            positions = []
            value = 0.0
            for token, pos in self.portfolio.positions.items():
                if not pos.size:
                    continue
                inst = self.instruments.get(token)
                shares = pos.size / SIZE_SCALE
                cost = pos.cost / CASH_SCALE
                mark = marks.get(token)
                current = shares * mark if mark is not None else cost
                value += current
                positions.append(
                    {
                        "token": token,
                        "label": inst.label if inst else token,
                        "outcome": inst.outcome if inst else "",
                        "event_slug": inst.event_slug if inst else "",
                        "shares": shares,
                        "avg_price": cost / shares,
                        "price": mark,
                        "value": current,
                        "pnl": current - cost,
                        "pnl_pct": (current - cost) / cost if cost else 0.0,
                    }
                )
            cash = self.portfolio.cash / CASH_SCALE
            start = self.portfolio.initial_cash / CASH_SCALE
            return {
                "cash": cash,
                "free_cash": self.free_cash() / CASH_SCALE,
                "positions_value": value,
                "equity": cash + value,
                "starting_cash": start,
                "pnl": cash + value - start,
                "realized": self.portfolio.realized() / CASH_SCALE,
                "positions": sorted(positions, key=lambda p: -p["value"]),
                "orders": [self._order_dict(o) for o in self.orders.values()],
            }
