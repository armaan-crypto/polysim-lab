"""Run metrics, and theoretical versus realized edge.

All money is in USD. Definitions (see docs/METRICS.md for the formulas):

- return on capital = net PnL / starting cash
- annualized return on locked capital = net PnL / mean locked capital, scaled to a year
- Sharpe = mean / sd of per-sample equity returns, times sqrt(samples per year)
- max drawdown = largest peak-to-trough fall of the equity curve
- slippage = theoretical edge - realized PnL
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from pmsim.book.units import CASH_SCALE
from pmsim.engine.backtest import BacktestResult
from pmsim.engine.portfolio import MS_PER_YEAR


@dataclass(frozen=True)
class Metrics:
    strategy: str
    label: str
    net_pnl_usd: float
    return_on_capital: float
    annualized_return_on_locked: float
    sharpe: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    hit_rate: float
    closed_positions: int
    avg_edge_per_entry_usd: float
    avg_pnl_per_position_usd: float
    fill_rate: float
    leg_failure_rate: float
    theoretical_edge_usd: float
    slippage_usd: float
    unwind_cost_usd: float
    resolution_losses: int
    idle_cash_fraction: float

    def as_dict(self) -> dict[str, float | int | str]:
        return asdict(self)


def usd(cash: int) -> float:
    return cash / CASH_SCALE


def max_drawdown(equity: list[tuple[int, int]]) -> tuple[float, float]:
    """(USD, fraction of the peak) of the worst peak-to-trough fall."""
    peak, worst, worst_pct = -math.inf, 0.0, 0.0
    for _, value in equity:
        peak = max(peak, value)
        fall = peak - value
        if fall > worst:
            worst, worst_pct = fall, fall / peak if peak > 0 else 0.0
    return usd(int(worst)), worst_pct


def sharpe(equity: list[tuple[int, int]]) -> float:
    """Annualized Sharpe of equity returns between samples (zero risk-free rate)."""
    if len(equity) < 3:
        return 0.0
    rets = [b / a - 1 for (_, a), (_, b) in zip(equity, equity[1:], strict=False) if a > 0]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    span = equity[-1][0] - equity[0][0]
    if sd == 0 or span <= 0:
        return 0.0
    per_year = len(rets) * MS_PER_YEAR / span
    return mean / sd * math.sqrt(per_year)


def annualized_on_locked(res: BacktestResult) -> float:
    """Net PnL over time-weighted locked capital, annualized (simple, not compounded)."""
    capital_ms = sum(c.capital_ms for c in res.closed)
    if not res.equity or capital_ms <= 0:
        return 0.0
    span = res.equity[-1][0] - res.equity[0][0]
    if span <= 0:
        return 0.0
    mean_locked = capital_ms / span
    return res.net_pnl / mean_locked * MS_PER_YEAR / span


def compute(res: BacktestResult) -> Metrics:
    trades = len(res.closed)
    wins = sum(1 for c in res.closed if c.pnl > 0)
    dd_usd, dd_pct = max_drawdown(res.equity)
    groups = res.legs.groups if res.legs else 0
    edge_orders = res.edge_orders
    return Metrics(
        strategy=res.strategy,
        label=res.label,
        net_pnl_usd=res.net_pnl_usd(),
        return_on_capital=res.net_pnl / res.start_cash,
        annualized_return_on_locked=annualized_on_locked(res),
        sharpe=sharpe(res.equity),
        max_drawdown_usd=dd_usd,
        max_drawdown_pct=dd_pct,
        hit_rate=wins / trades if trades else 0.0,
        closed_positions=trades,
        avg_edge_per_entry_usd=usd(res.expected_edge) / edge_orders if edge_orders else 0.0,
        avg_pnl_per_position_usd=usd(sum(c.pnl for c in res.closed)) / trades if trades else 0.0,
        fill_rate=res.orders_filled / res.orders if res.orders else 0.0,
        leg_failure_rate=res.legs.failed / groups if res.legs and groups else 0.0,
        theoretical_edge_usd=usd(res.expected_edge),
        slippage_usd=usd(res.expected_edge - res.net_pnl),
        unwind_cost_usd=usd(res.unwind_cost),
        resolution_losses=res.resolution_losses,
        idle_cash_fraction=res.idle_cash_fraction,
    )


@dataclass(frozen=True)
class EdgeComparison:
    strategy: str
    label: str
    theoretical_usd: float
    realized_usd: float

    @property
    def capture(self) -> float:
        """Realized as a fraction of theoretical edge (1.0 = all of it)."""
        return self.realized_usd / self.theoretical_usd if self.theoretical_usd else 0.0


def edge_table(results: list[BacktestResult]) -> list[EdgeComparison]:
    return [
        EdgeComparison(r.strategy, r.label, usd(r.expected_edge), r.net_pnl_usd()) for r in results
    ]
