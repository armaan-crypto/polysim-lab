"""Cash, positions, average cost, PnL and capital lock-up, in exact cash units.

Buy fees and gas go into cost basis; sell fees and gas reduce realized PnL.
That keeps the identity cash + total cost basis == initial cash + realized PnL.
Polymarket has no short selling, so positions never go below zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pmsim.book.units import PRICE_SCALE
from pmsim.engine.fills import Fill

MS_PER_YEAR = 365 * 24 * 3600 * 1000


class PortfolioError(ValueError):
    """An operation the venue would refuse (oversell, missing cash, time going back)."""


@dataclass
class Position:
    token: str
    size: int = 0
    cost: int = 0
    realized: int = 0
    opened_ts: int | None = None
    capital_ms: int = 0  # integral of cost basis over time

    def unrealized(self, mark: int) -> int:
        return self.size * mark - self.cost


@dataclass(frozen=True)
class ClosedPosition:
    token: str
    opened_ts: int
    closed_ts: int
    pnl: int
    capital_ms: int

    def holding_ms(self) -> int:
        return self.closed_ts - self.opened_ts

    def avg_capital(self) -> float:
        held = self.holding_ms()
        return self.capital_ms / held if held > 0 else 0.0

    def annualized_return(self) -> float:
        """Simple annualized return on time-weighted capital: (pnl / capital) * (year / held)."""
        held, cap = self.holding_ms(), self.avg_capital()
        if held <= 0 or cap <= 0:
            return 0.0
        return self.pnl / cap * MS_PER_YEAR / held


@dataclass
class Portfolio:
    cash: int
    now: int = 0
    positions: dict[str, Position] = field(default_factory=dict)
    closed: list[ClosedPosition] = field(default_factory=list)
    cash_ms: int = 0  # integral of idle cash over time
    deployed_ms: int = 0  # integral of total cost basis over time
    initial_cash: int = 0

    def __post_init__(self) -> None:
        self.initial_cash = self.cash

    # ---- queries ------------------------------------------------------------

    def position(self, token: str) -> Position:
        return self.positions.setdefault(token, Position(token))

    def size(self, token: str) -> int:
        pos = self.positions.get(token)
        return pos.size if pos else 0

    def realized(self) -> int:
        return sum(p.realized for p in self.positions.values())

    def unrealized(self, marks: Mapping[str, int]) -> int:
        return sum(p.unrealized(marks[t]) for t, p in self.positions.items() if p.size)

    def equity(self, marks: Mapping[str, int]) -> int:
        return self.cash + sum(p.size * marks[t] for t, p in self.positions.items() if p.size)

    def idle_cash_fraction(self) -> float:
        total = self.cash_ms + self.deployed_ms
        return self.cash_ms / total if total else 1.0

    # ---- updates ------------------------------------------------------------

    def advance(self, ts: int) -> None:
        """Accrue cash and capital time up to `ts`."""
        if ts < self.now:
            raise PortfolioError(f"time went back: {ts} < {self.now}")
        dt = ts - self.now
        self.cash_ms += self.cash * dt
        for pos in self.positions.values():
            pos.capital_ms += pos.cost * dt
            self.deployed_ms += pos.cost * dt
        self.now = ts

    def apply_fill(self, fill: Fill) -> None:
        self.advance(fill.ts)
        if fill.side == "BUY":
            self._buy(fill.token, fill.size, fill.price * fill.size + fill.fee + fill.gas)
        else:
            self._sell(fill.token, fill.size, fill.price * fill.size - fill.fee - fill.gas)

    def redeem(self, token: str, payout: int, ts: int, cost: int = 0) -> None:
        """Turn the whole position into cash at `payout` price units per share."""
        self.advance(ts)
        size = self.size(token)
        if size:
            self._sell(token, size, size * payout - cost)

    def refund(self, token: str, ts: int) -> None:
        """Voided market: return the cost basis, booking zero PnL."""
        self.advance(ts)
        size = self.size(token)
        if size:
            pos, basis = self._take(token, size)
            self.cash += basis
            self._book(pos, 0)

    def merge(self, yes: str, no: str, size: int, ts: int, cost: int = 0) -> None:
        """YES plus NO of the same market merge into 1 USDC per share."""
        self.advance(ts)
        self._check_holds([yes, no], size)
        yes_pos, yes_basis = self._take(yes, size)
        no_pos, no_basis = self._take(no, size)
        value = size * PRICE_SCALE - cost
        half = value // 2
        self.cash += value
        self._book(yes_pos, half - yes_basis)
        self._book(no_pos, value - half - no_basis)

    def convert_no(
        self, no_token: str, other_yes: Sequence[str], size: int, ts: int, cost: int = 0
    ) -> None:
        """Neg-risk conversion: NO on one outcome becomes YES on every other outcome.

        The NO cost basis carries over to the new YES positions, split evenly,
        so the conversion books no PnL beyond its own `cost`.
        """
        if not other_yes:
            raise PortfolioError("conversion needs at least one other outcome")
        self.advance(ts)
        no_pos, basis = self._take(no_token, size)
        self.cash -= cost
        self._book(no_pos, -cost)
        share, extra = divmod(basis, len(other_yes))
        for i, token in enumerate(other_yes):
            self._add(token, size, share + (1 if i < extra else 0))

    # ---- internals ----------------------------------------------------------

    def _check_holds(self, tokens: Sequence[str], size: int) -> None:
        for token in tokens:
            if size <= 0 or self.size(token) < size:
                raise PortfolioError(f"hold {self.size(token)} of {token}, need {size}")

    def _buy(self, token: str, size: int, total_cost: int) -> None:
        if total_cost > self.cash:
            raise PortfolioError(f"buy costs {total_cost}, cash is {self.cash}")
        self.cash -= total_cost
        self._add(token, size, total_cost)

    def _sell(self, token: str, size: int, proceeds: int) -> None:
        pos, basis = self._take(token, size)
        self.cash += proceeds
        self._book(pos, proceeds - basis)

    def _add(self, token: str, size: int, basis: int) -> None:
        pos = self.position(token)
        if pos.size == 0:
            pos.opened_ts = self.now
        pos.size += size
        pos.cost += basis

    def _take(self, token: str, size: int) -> tuple[Position, int]:
        """Remove `size` shares at average cost; return the position and basis removed."""
        self._check_holds([token], size)
        pos = self.positions[token]
        basis = pos.cost if size == pos.size else pos.cost * size // pos.size
        pos.size -= size
        pos.cost -= basis
        return pos, basis

    def _book(self, pos: Position, pnl: int) -> None:
        pos.realized += pnl
        if pos.size == 0:
            self._close(pos)

    def _close(self, pos: Position) -> None:
        opened = pos.opened_ts if pos.opened_ts is not None else self.now
        prior = sum(c.pnl for c in self.closed if c.token == pos.token)
        self.closed.append(
            ClosedPosition(pos.token, opened, self.now, pos.realized - prior, pos.capital_ms)
        )
        pos.capital_ms = 0
        pos.opened_ts = None
