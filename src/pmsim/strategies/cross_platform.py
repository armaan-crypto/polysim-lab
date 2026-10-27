"""Strategy 6: cross-platform spread between Polymarket and Kalshi.

For the same question on both venues, YES on one plus NO on the other pays
$1 if both resolve the same way:

    edge = 1 - (P_YES^poly + P_NO^kalshi) - fees - gas - lambda * m

where m in [0, 1] is the resolution-rule mismatch score and lambda a penalty
in dollars per share. Pairs above `max_mismatch` are not traded. Kalshi legs
go out as `ExternalIntent`s and are filled by a mock venue in simulation.
The Kalshi fee rate is an assumption (see Needs live verification).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.units import PRICE_SCALE, size_units
from pmsim.data.kalshi import QuoteFeed
from pmsim.data.kalshi import leg_token as kalshi_leg
from pmsim.engine.fees import FeeModel
from pmsim.engine.fills import Fill
from pmsim.strategies.base import BookUpdate, ExternalIntent, Intent, OrderIntent
from pmsim.strategies.costs import buy_cost, per_share
from pmsim.strategies.legs import LegManager, LegStrategy


class CrossPair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    poly_yes: str
    poly_no: str
    kalshi_ticker: str
    mismatch: float = Field(ge=0, le=1)


class CrossPlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pairs: list[CrossPair] = Field(default_factory=list)
    min_edge_usd_per_share: float = Field(
        default=0.01,
        ge=0,
        title="Minimum profit per share",
        description="Only trade when both legs lock in at least this many dollars per share.",
    )
    max_mismatch: float = Field(
        default=0.3,
        ge=0,
        le=1,
        title="Rules mismatch limit",
        description="Skip pairs whose resolution rules differ more than this (0 to 1).",
    )
    mismatch_penalty_usd: float = Field(
        default=0.05,
        ge=0,
        title="Mismatch penalty",
        description="Dollars per share subtracted for each unit of rules mismatch.",
    )
    kalshi_fee_rate: float = Field(
        default=0.07,
        ge=0,
        title="Kalshi fee rate",
        description="Assumed Kalshi taker fee rate (unverified).",
    )  # assumed, same form as Polymarket
    max_shares: float = Field(
        default=100,
        gt=0,
        title="Shares per trade",
        description="Largest number of pairs bought in one go.",
    )
    leg_timeout_ms: int = Field(
        default=3_000,
        gt=0,
        title="Leg timeout (ms)",
        description="If only one leg fills within this time, sell the extra back.",
    )


class CrossPlatformSpread(LegStrategy):
    name = "cross_platform"

    def __init__(self, feed: QuoteFeed, cfg: CrossPlatformConfig | None = None) -> None:
        self.cfg = cfg or CrossPlatformConfig()
        self.feed = feed
        self.legs = LegManager(self.cfg.leg_timeout_ms)
        self._kalshi_fees = FeeModel(self.cfg.kalshi_fee_rate)

    def on_book(self, update: BookUpdate) -> list[Intent]:
        out: list[Intent] = []
        for pair in self.cfg.pairs:
            if update.token in (pair.poly_yes, pair.poly_no):
                out.extend(self.check(pair))
        return out

    def on_timer(self, t: int) -> list[Intent]:
        out = super().on_timer(t)
        for pair in self.cfg.pairs:
            out.extend(self.check(pair))
        return out

    def on_external_fill(self, intent: ExternalIntent, size: int, price: int) -> list[Intent]:
        token = kalshi_leg(intent.ticker, intent.outcome)
        fee = self._kalshi_fees.fee(price, size, "taker")
        fill = Fill(0, token, "BUY", price, size, self.view.now, "taker", fee, 0)
        leg = OrderIntent(token, "BUY", price, size, "IOC", intent.group, "leg")
        self.legs.on_fill(fill, leg)
        return []

    def check(self, pair: CrossPair) -> list[Intent]:
        if pair.mismatch > self.cfg.max_mismatch:
            return []
        quote = self.feed.quote(pair.kalshi_ticker, self.view.now)
        if quote is None:
            return []
        self.view.clock.check(quote.ts, "kalshi quote")
        # (poly token, kalshi outcome, kalshi price, kalshi size)
        options = [
            (pair.poly_yes, "no", quote.no_ask, quote.yes_bid_size),
            (pair.poly_no, "yes", quote.yes_ask, quote.yes_ask_size),
        ]
        for poly, outcome, k_px, k_sz in options:
            intents = self._try(pair, poly, outcome, k_px, k_sz)
            if intents:
                return intents
        return []

    def _try(self, pair: CrossPair, poly: str, outcome: str, k_px: int, k_sz: int) -> list[Intent]:
        view = self.view
        k_token = kalshi_leg(pair.kalshi_ticker, outcome)
        book = view.book(poly)
        if book is None or book.best_ask is None or self.legs.busy([poly, k_token]):
            return []
        if not 0 < k_px < PRICE_SCALE:
            return []
        p_px, p_sz = book.best_ask
        size = min(p_sz, k_sz, size_units(self.cfg.max_shares))
        rules = view.rules[poly]
        if size < rules.min_size:
            return []
        cost = buy_cost(p_px, size, rules, view.gas_cash)
        cost += k_px * size + self._kalshi_fees.fee(k_px, size, "taker")
        penalty = per_share(self.cfg.mismatch_penalty_usd * pair.mismatch, size)
        edge = PRICE_SCALE * size - cost - penalty
        if edge <= 0 or edge < per_share(self.cfg.min_edge_usd_per_share, size):
            return []
        legs = [
            OrderIntent(poly, "BUY", p_px, size, "IOC"),
            OrderIntent(k_token, "BUY", k_px, size),
        ]
        tagged = self.legs.open(f"{self.name}-{pair.name}", legs, edge, view.now)
        poly_leg = tagged[0]
        ext = ExternalIntent(
            "kalshi", pair.kalshi_ticker, "BUY", outcome, k_px, size, poly_leg.group
        )
        return [poly_leg, ext]
