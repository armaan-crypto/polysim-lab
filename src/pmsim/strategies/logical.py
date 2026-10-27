"""Strategy 4: logical arbitrage across related markets.

If event A implies event B (A is a subset of B), then P(A) <= P(B). When the
market prices A above B, buy NO on A and YES on B. The pair pays at least $1
in every world:

    A true  (so B true):  NO_A = 0, YES_B = 1  -> 1
    A false, B true:      NO_A = 1, YES_B = 1  -> 2
    A false, B false:     NO_A = 1, YES_B = 0  -> 1

    edge >= 1 - (P_NO_A + P_YES_B) - fees - gas

Relations are curated by hand in config; nothing infers them from text.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.units import size_units
from pmsim.strategies.base import BookUpdate, Intent, OrderIntent
from pmsim.strategies.costs import per_share, set_edge
from pmsim.strategies.legs import LegManager, LegStrategy


class Relation(BaseModel):
    """`subset` implies `superset`. Tokens are the NO of the subset and YES of the superset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    subset_no: str
    superset_yes: str


class LogicalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relations: list[Relation] = Field(default_factory=list)
    min_edge_usd_per_share: float = Field(
        default=0.01,
        ge=0,
        title="Minimum profit per share",
        description="Only trade when the pair is mispriced by at least this many dollars.",
    )
    max_shares: float = Field(
        default=100,
        gt=0,
        title="Shares per trade",
        description="Largest number of pairs bought in one go.",
    )
    leg_timeout_ms: int = Field(
        default=2_000,
        gt=0,
        title="Leg timeout (ms)",
        description="If only one side fills within this time, sell the extra side back.",
    )


class LogicalArb(LegStrategy):
    name = "logical"

    def __init__(self, cfg: LogicalConfig | None = None) -> None:
        self.cfg = cfg or LogicalConfig()
        self.legs = LegManager(self.cfg.leg_timeout_ms)

    def on_book(self, update: BookUpdate) -> list[Intent]:
        out: list[Intent] = []
        for rel in self.cfg.relations:
            if update.token in (rel.subset_no, rel.superset_yes):
                out.extend(self._check(rel))
        return out

    def _check(self, rel: Relation) -> list[Intent]:
        view = self.view
        tokens = [rel.subset_no, rel.superset_yes]
        if self.legs.busy(tokens):
            return []
        books = [view.book(t) for t in tokens]
        asks = [b.best_ask for b in books if b is not None and b.best_ask is not None]
        if len(asks) != 2:
            return []
        size = min(asks[0][1], asks[1][1], size_units(self.cfg.max_shares))
        if size < max(view.rules[t].min_size for t in tokens):
            return []
        legs = [(asks[0][0], view.rules[tokens[0]]), (asks[1][0], view.rules[tokens[1]])]
        edge = set_edge(legs, size, 1, view.gas_cash)
        if edge <= 0 or edge < per_share(self.cfg.min_edge_usd_per_share, size):
            return []
        intents = [
            OrderIntent(t, "BUY", a[0], size, "IOC") for t, a in zip(tokens, asks, strict=True)
        ]
        return list(self.legs.open(f"{self.name}-{rel.name}", intents, edge, view.now))
