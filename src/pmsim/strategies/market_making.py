"""Strategy 5: market making with inventory limits and spread targets.

Quotes around the mid, skewed against inventory:

    fair = mid - k * q,   bid = fair - h,   ask = fair + h

with q the inventory in 100-share units, k the skew in ticks per unit and h
the half spread. Quotes never cross the book (post only). Polymarket has no
short selling, so the ask only offers inventory already held.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.units import PRICE_SCALE, SIZE_SCALE, Side, size_units
from pmsim.strategies.base import BookUpdate, CancelIntent, Intent, OrderIntent, Strategy
from pmsim.strategies.costs import snap_down, snap_up


class MarketMakingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens: list[str] = Field(default_factory=list)  # empty quotes every token seen
    half_spread_ticks: int = Field(default=2, ge=1)
    quote_shares: float = Field(default=50, gt=0)
    max_inventory_shares: float = Field(default=500, gt=0)
    skew_ticks_per_100: float = Field(default=1.0, ge=0)
    requote_ticks: int = Field(default=1, ge=1)


class MarketMaker(Strategy):
    name = "market_making"

    def __init__(self, cfg: MarketMakingConfig | None = None) -> None:
        self.cfg = cfg or MarketMakingConfig()
        self._cancelling: set[int] = set()

    def quotes(self, token: str) -> dict[Side, tuple[int, int]]:
        """Desired (price, size) per side, empty when there is nothing to quote."""
        view, cfg = self.view, self.cfg
        book = view.book(token)
        if book is None or book.best_bid is None or book.best_ask is None:
            return {}
        tick = view.rules[token].tick
        inv = view.position(token)
        fair = (book.best_bid[0] + book.best_ask[0]) / 2
        fair -= cfg.skew_ticks_per_100 * tick * inv / (100 * SIZE_SCALE)
        bid = min(snap_down(fair - cfg.half_spread_ticks * tick, tick), book.best_ask[0] - tick)
        ask = max(snap_up(fair + cfg.half_spread_ticks * tick, tick), book.best_bid[0] + tick)
        size = size_units(cfg.quote_shares)
        dying = {"BUY": 0, "SELL": 0}  # orders being cancelled can still fill
        for order_id, side, _, remaining in view.open_orders(token):
            if order_id in self._cancelling:
                dying[side] += remaining
        out: dict[Side, tuple[int, int]] = {}
        if inv + dying["BUY"] + size <= size_units(cfg.max_inventory_shares) and tick <= bid:
            out["BUY"] = (bid, size)
        sell = min(size, inv - dying["SELL"])
        if sell >= view.rules[token].min_size and ask <= PRICE_SCALE - tick:
            out["SELL"] = (ask, sell)
        return out

    def on_book(self, update: BookUpdate) -> list[Intent]:
        token = update.token
        if self.cfg.tokens and token not in self.cfg.tokens:
            return []
        want = self.quotes(token)
        tick = self.view.rules[token].tick
        out: list[Intent] = []
        live: set[Side] = set()
        for order_id, side, price, _ in self.view.open_orders(token):
            if order_id in self._cancelling:
                continue
            target = want.get(side)
            if target is None or abs(target[0] - price) >= self.cfg.requote_ticks * tick:
                self._cancelling.add(order_id)
                out.append(CancelIntent(order_id))
            else:
                live.add(side)
        want = self.quotes(token)  # resize now that the new cancels count as dying
        for side, (price, size) in want.items():
            if side not in live:
                out.append(OrderIntent(token, side, price, size, "GTC", tag="quote"))
        return out
