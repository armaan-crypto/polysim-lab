"""Multi-leg execution with leg risk.

Legs of a group go out together. If, by the deadline, the legs filled
unequal sizes, the excess on the over-filled legs is sold back at the bid
(an unwind). Unwind cost is the basis of the excess minus what selling it
fetched, fees included.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from pmsim.book.units import PRICE_SCALE
from pmsim.engine.fills import Fill
from pmsim.strategies.base import Intent, MarketView, OrderIntent, Strategy


@dataclass
class LegGroup:
    id: str
    legs: tuple[OrderIntent, ...]
    deadline: int
    expected_edge: int
    filled: dict[str, int] = field(default_factory=dict)
    paid: dict[str, int] = field(default_factory=dict)  # cash spent per leg, fees included
    unwind_proceeds: int = 0
    unwind_basis: int = 0
    state: str = "open"  # open, complete, failed

    def imbalance(self) -> dict[str, int]:
        """Shares per leg above the smallest leg fill."""
        done = min(self.filled.get(leg.token, 0) for leg in self.legs)
        return {leg.token: self.filled.get(leg.token, 0) - done for leg in self.legs}

    def unwind_cost(self) -> int:
        return self.unwind_basis - self.unwind_proceeds


@dataclass
class LegStats:
    groups: int = 0
    complete: int = 0
    failed: int = 0


class LegManager:
    def __init__(self, timeout_ms: int = 2_000, unwind_ticks: int = 1) -> None:
        self.timeout_ms = timeout_ms
        self.unwind_ticks = unwind_ticks
        self.groups: dict[str, LegGroup] = {}
        self.stats = LegStats()
        self._n = 0

    def open(
        self, prefix: str, legs: Sequence[OrderIntent], edge: int, now: int
    ) -> list[OrderIntent]:
        """Register a group and return its leg intents, tagged with the group id."""
        self._n += 1
        gid = f"{prefix}-{self._n}"
        tagged = tuple(
            OrderIntent(
                leg.token,
                leg.side,
                leg.price,
                leg.size,
                leg.tif,
                gid,
                "leg",
                edge if i == 0 else 0,
            )
            for i, leg in enumerate(legs)
        )
        self.groups[gid] = LegGroup(gid, tagged, now + self.timeout_ms, edge)
        self.stats.groups += 1
        return list(tagged)

    def busy(self, tokens: Sequence[str]) -> bool:
        """True if an open group already trades any of `tokens`."""
        return any(
            g.state == "open" and any(leg.token in tokens for leg in g.legs)
            for g in self.groups.values()
        )

    def on_fill(self, fill: Fill, intent: OrderIntent) -> None:
        group = self.groups.get(intent.group)
        if group is None:
            return
        if intent.tag == "unwind":
            group.unwind_proceeds += -fill.cash_delta
            return
        group.filled[fill.token] = group.filled.get(fill.token, 0) + fill.size
        group.paid[fill.token] = group.paid.get(fill.token, 0) - fill.cash_delta
        if all(group.filled.get(leg.token, 0) == leg.size for leg in group.legs):
            group.state = "complete"
            self.stats.complete += 1

    def on_timer(self, view: MarketView) -> list[Intent]:
        """Unwind groups past their deadline with unequal leg fills."""
        out: list[Intent] = []
        for group in self.groups.values():
            if group.state != "open" or view.now < group.deadline:
                continue
            excess = {t: q for t, q in group.imbalance().items() if q > 0}
            group.state = "failed" if excess else "complete"
            if not excess:
                self.stats.complete += 1
                continue
            self.stats.failed += 1
            for token, qty in excess.items():
                out.extend(self._unwind(view, group, token, qty))
        return out

    def unwind_cost(self) -> int:
        """Total unwind cost so far. Unsold excess counts as a full loss of its basis."""
        return sum(g.unwind_cost() for g in self.groups.values() if g.state == "failed")

    def _unwind(self, view: MarketView, group: LegGroup, token: str, qty: int) -> list[Intent]:
        book = view.book(token)
        if book is None or book.best_bid is None:
            return []  # nothing to sell into: the excess is held to resolution
        paid, filled = group.paid.get(token, 0), group.filled.get(token, 0)
        group.unwind_basis += paid * qty // filled
        tick = view.rules[token].tick
        price = max(tick, book.best_bid[0] - self.unwind_ticks * tick)
        price = min(price, PRICE_SCALE - tick)
        return [OrderIntent(token, "SELL", price, qty, "IOC", group.id, "unwind")]


class LegStrategy(Strategy):
    """A strategy that trades multi-leg groups through a `LegManager`."""

    legs: LegManager

    def on_timer(self, t: int) -> list[Intent]:
        return self.legs.on_timer(self.view)

    def on_fill(self, fill: Fill, intent: OrderIntent) -> list[Intent]:
        self.legs.on_fill(fill, intent)
        return []
