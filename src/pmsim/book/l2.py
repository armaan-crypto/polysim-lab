"""Price-level (L2) book rebuilt from recorded snapshots and deltas."""

from __future__ import annotations

from collections.abc import Iterable

from pmsim.book.units import Side

Level = tuple[int, int]  # (price units, size units)


class L2Book:
    """Aggregated depth per price. BUY levels are bids, SELL levels are asks."""

    def __init__(self) -> None:
        self._levels: dict[Side, dict[int, int]] = {"BUY": {}, "SELL": {}}

    def apply_snapshot(self, bids: Iterable[Level], asks: Iterable[Level]) -> None:
        self._levels = {
            "BUY": {p: s for p, s in bids if s > 0},
            "SELL": {p: s for p, s in asks if s > 0},
        }
        self._uncross("BUY")

    def apply_delta(self, side: Side, price: int, size: int) -> list[Level]:
        """Set the size at one level (0 removes it).

        Recorded feeds can briefly cross when deltas for the two sides arrive
        out of order. The newest level wins: opposite levels it crosses are
        dropped and returned.
        """
        if size < 0:
            raise ValueError("negative level size")
        book = self._levels[side]
        if size == 0:
            book.pop(price, None)
            return []
        book[price] = size
        return self._uncross(side)

    def best(self, side: Side) -> int | None:
        book = self._levels[side]
        if not book:
            return None
        return max(book) if side == "BUY" else min(book)

    def size_at(self, side: Side, price: int) -> int:
        return self._levels[side].get(price, 0)

    def levels(self, side: Side) -> list[Level]:
        """Levels best first."""
        return sorted(self._levels[side].items(), reverse=side == "BUY")

    def is_crossed(self) -> bool:
        bid, ask = self.best("BUY"), self.best("SELL")
        return bid is not None and ask is not None and bid >= ask

    def _uncross(self, newest: Side) -> list[Level]:
        bid, ask = self.best("BUY"), self.best("SELL")
        if bid is None or ask is None or bid < ask:
            return []
        if newest == "BUY":
            gone = [(p, s) for p, s in self._levels["SELL"].items() if p <= bid]
            stale: Side = "SELL"
        else:
            gone = [(p, s) for p, s in self._levels["BUY"].items() if p >= ask]
            stale = "BUY"
        for p, _ in gone:
            del self._levels[stale][p]
        return gone
