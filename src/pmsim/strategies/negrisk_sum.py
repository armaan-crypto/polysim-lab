"""Strategy 2: sum arbitrage on neg-risk (mutually exclusive) groups.

Exactly one market in a group resolves YES, so:

- YES set: buying one YES in every market pays exactly $1.
  edge = 1 - sum(P_YES) - fees - gas
- NO set: buying one NO in every market pays exactly $(n - 1).
  edge = (n - 1) - sum(P_NO) - fees - gas. This is the trade when sum(P_YES) is above 1,
  since Polymarket has no short selling.

Groups with an explicit "Other" market are skipped: the docs advise against trading it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.units import size_units
from pmsim.risk.negrisk import NegRiskGroup
from pmsim.strategies.base import BookUpdate, Intent, OrderIntent
from pmsim.strategies.costs import per_share, set_edge
from pmsim.strategies.legs import LegManager, LegStrategy

SetSide = Literal["yes", "no"]


class NegRiskSumConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_edge_usd_per_set: float = Field(
        default=0.01,
        ge=0,
        title="Minimum profit per set",
        description="Only trade when a full set locks in at least this many dollars.",
    )
    max_sets: float = Field(
        default=50,
        gt=0,
        title="Sets per trade",
        description="Largest number of complete sets bought in one go.",
    )
    leg_timeout_ms: int = Field(
        default=3_000,
        gt=0,
        title="Leg timeout (ms)",
        description="If some outcomes fail to fill within this time, sell the extras back.",
    )
    trade_no_sets: bool = Field(
        default=True,
        title="Also trade NO sets",
        description="Buy every NO when YES prices add up to more than $1.",
    )


class NegRiskSumArb(LegStrategy):
    name = "negrisk_sum"

    def __init__(self, cfg: NegRiskSumConfig | None = None) -> None:
        self.cfg = cfg or NegRiskSumConfig()
        self.legs = LegManager(self.cfg.leg_timeout_ms)

    def on_book(self, update: BookUpdate) -> list[Intent]:
        meta = self.view.meta.get(update.token)
        if meta is None or not meta.group_id:
            return []
        group = next((g for g in self.view.groups if g.group_id == meta.group_id), None)
        if group is None or any(m.is_other for m in group.markets) or len(group.markets) < 2:
            return []
        sides: list[SetSide] = ["yes", "no"] if self.cfg.trade_no_sets else ["yes"]
        for side in sides:
            intents = self._try_set(group, side)
            if intents:
                return intents
        return []

    def _try_set(self, group: NegRiskGroup, side: SetSide) -> list[Intent]:
        view = self.view
        tokens = [m.yes if side == "yes" else m.no for m in group.markets]
        if self.legs.busy(tokens):
            return []
        asks = []
        for token in tokens:
            book = view.book(token)
            if book is None or book.best_ask is None:
                return []
            asks.append(book.best_ask)
        size = min(min(sz for _, sz in asks), size_units(self.cfg.max_sets))
        if size < max(view.rules[t].min_size for t in tokens):
            return []
        payout = 1 if side == "yes" else len(tokens) - 1
        legs = [(px, view.rules[t]) for (px, _), t in zip(asks, tokens, strict=True)]
        edge = set_edge(legs, size, payout, view.gas_cash)
        if edge <= 0 or edge < per_share(self.cfg.min_edge_usd_per_set, size):
            return []
        intents = [
            OrderIntent(t, "BUY", px, size, "IOC") for (px, _), t in zip(asks, tokens, strict=True)
        ]
        return list(self.legs.open(f"{self.name}-{side}", intents, edge, view.now))
