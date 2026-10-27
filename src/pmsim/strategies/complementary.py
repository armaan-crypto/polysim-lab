"""Strategy 1: complementary arbitrage on one binary market.

Buy YES and NO together when the pair costs less than $1 after costs:

    edge = 1 - (P_YES + P_NO) - fees - gas   (per share, both legs taken at the ask)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.units import size_units
from pmsim.strategies.base import BookUpdate, Intent, OrderIntent
from pmsim.strategies.costs import per_share, set_edge
from pmsim.strategies.legs import LegManager, LegStrategy


class ComplementaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_edge_usd_per_share: float = Field(
        default=0.005,
        ge=0,
        title="Minimum profit per share",
        description="Only trade when the pair locks in this many dollars per share after fees.",
    )
    max_shares: float = Field(
        default=100,
        gt=0,
        title="Shares per trade",
        description="Largest number of YES+NO pairs bought in one go.",
    )
    max_position_shares: float = Field(
        default=1_000,
        gt=0,
        title="Position limit",
        description="Stop buying a market once you hold this many shares of either side.",
    )
    leg_timeout_ms: int = Field(
        default=2_000,
        gt=0,
        title="Leg timeout (ms)",
        description="If only one side fills within this time, sell the extra side back.",
    )


class ComplementaryArb(LegStrategy):
    name = "complementary"

    def __init__(self, cfg: ComplementaryConfig | None = None) -> None:
        self.cfg = cfg or ComplementaryConfig()
        self.legs = LegManager(self.cfg.leg_timeout_ms)

    def on_book(self, update: BookUpdate) -> list[Intent]:
        view = self.view
        meta = view.meta.get(update.token)
        if meta is None:
            return []
        yes, no = sorted([meta.token, meta.complement], key=lambda t: view.meta[t].outcome_index)
        if self.legs.busy([yes, no]):
            return []
        ya, na = view.book(yes), view.book(no)
        if ya is None or na is None or ya.best_ask is None or na.best_ask is None:
            return []
        (y_px, y_sz), (n_px, n_sz) = ya.best_ask, na.best_ask
        room = size_units(self.cfg.max_position_shares) - max(view.position(yes), view.position(no))
        size = min(y_sz, n_sz, size_units(self.cfg.max_shares), room)
        if size < max(view.rules[yes].min_size, view.rules[no].min_size):
            return []
        legs = [(y_px, view.rules[yes]), (n_px, view.rules[no])]
        edge = set_edge(legs, size, 1, view.gas_cash)
        if edge <= 0 or edge < per_share(self.cfg.min_edge_usd_per_share, size):
            return []
        intents = [
            OrderIntent(yes, "BUY", y_px, size, "IOC"),
            OrderIntent(no, "BUY", n_px, size, "IOC"),
        ]
        return list(self.legs.open(self.name, intents, edge, view.now))
