"""Strategy 3: near-resolution yield.

Buy outcomes priced 0.95 to 0.99 shortly before the market ends, when the
rules text looks clean. If it wins, the yield on capital is

    y = (1 - p - c) / (p + c)   annualized over the hours left plus a resolution buffer,

where c is fees and gas per share. A loss costs about p per share, so this
is a bet that the rules hold, not an arbitrage.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.units import PRICE_SCALE, size_units
from pmsim.engine.portfolio import MS_PER_YEAR
from pmsim.strategies.base import BookUpdate, Intent, OrderIntent, Strategy
from pmsim.strategies.costs import buy_cost
from pmsim.strategies.rules_text import risk_score


class NearResolutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_price: float = Field(
        default=0.95,
        gt=0,
        lt=1,
        title="Lowest price",
        description="Ignore outcomes cheaper than this (0.95 = 95 cents).",
    )
    max_price: float = Field(
        default=0.99,
        gt=0,
        lt=1,
        title="Highest price",
        description="Ignore outcomes more expensive than this; little left to earn.",
    )
    max_hours_to_end: float = Field(
        default=72,
        gt=0,
        title="Hours before end",
        description="Only buy markets ending within this many hours.",
    )
    resolution_buffer_hours: float = Field(
        default=2,
        ge=0,
        title="Payout delay (hours)",
        description="Expected wait after the end date before winners are paid.",
    )  # UMA challenge period
    min_annualized_yield: float = Field(
        default=0.20,
        ge=0,
        title="Minimum yearly yield",
        description="Skip trades whose yearly return would be below this (0.2 = 20%).",
    )
    max_risk_score: float = Field(
        default=1.0,
        ge=0,
        title="Rules risk limit",
        description="Skip markets whose rules mention risky words like postponed or 50-50.",
    )
    max_shares: float = Field(
        default=200,
        gt=0,
        title="Shares per trade",
        description="Largest number of shares bought in one go.",
    )
    max_position_shares: float = Field(
        default=1_000,
        gt=0,
        title="Position limit",
        description="Stop buying a market once you hold this many shares.",
    )


class NearResolutionYield(Strategy):
    name = "near_resolution"

    def __init__(self, cfg: NearResolutionConfig | None = None) -> None:
        self.cfg = cfg or NearResolutionConfig()

    def on_book(self, update: BookUpdate) -> list[Intent]:
        view, cfg = self.view, self.cfg
        token = update.token
        meta = view.meta.get(token)
        hours = view.hours_to_end(token) if meta else None
        if meta is None or hours is None or not 0 < hours <= cfg.max_hours_to_end:
            return []
        if risk_score(meta.rules_text) > cfg.max_risk_score or view.open_orders(token):
            return []
        book = view.book(token)
        if book is None or book.best_ask is None:
            return []
        price, avail = book.best_ask
        if not round(cfg.min_price * PRICE_SCALE) <= price <= round(cfg.max_price * PRICE_SCALE):
            return []
        room = size_units(cfg.max_position_shares) - view.position(token)
        size = min(avail, size_units(cfg.max_shares), room)
        rules = view.rules[token]
        if size < rules.min_size:
            return []
        cost = buy_cost(price, size, rules, view.gas_cash)
        edge = PRICE_SCALE * size - cost
        held_ms = (hours + cfg.resolution_buffer_hours) * 3_600_000
        if edge <= 0 or edge / cost * MS_PER_YEAR / held_ms < cfg.min_annualized_yield:
            return []
        return [OrderIntent(token, "BUY", price, size, "IOC", tag="yield", edge=edge)]
