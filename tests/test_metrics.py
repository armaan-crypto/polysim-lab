from collections import Counter
from pathlib import Path

import pytest

from pmsim.analysis.metrics import (
    annualized_on_locked,
    compute,
    edge_table,
    max_drawdown,
    sharpe,
    usd,
)
from pmsim.analysis.report import render, write_report
from pmsim.book.units import CASH_SCALE
from pmsim.data.synthetic import SyntheticConfig
from pmsim.engine.backtest import BacktestResult, Decision
from pmsim.engine.experiment import RunSpec, run_full
from pmsim.engine.fills import Fill
from pmsim.engine.portfolio import MS_PER_YEAR, ClosedPosition
from pmsim.strategies.legs import LegStats

USD = CASH_SCALE
DAY = MS_PER_YEAR // 365


def result(**kw: object) -> BacktestResult:
    base: dict[str, object] = dict(
        strategy="s",
        label="synthetic",
        start_cash=1000 * USD,
        final_equity=1100 * USD,
        realized=100 * USD,
        fills=[],
        expected_edge=120 * USD,
        intents=0,
        rejects=Counter(),
        equity=[(0, 1000 * USD), (DAY, 1050 * USD), (2 * DAY, 1100 * USD)],
        closed=[ClosedPosition("a", 0, 2 * DAY, 100 * USD, 500 * USD * 2 * DAY)],
        legs=LegStats(groups=4, complete=3, failed=1),
        unwind_cost=5 * USD,
        idle_cash_fraction=0.5,
        resolutions=1,
        resolution_losses=0,
        orders=10,
        orders_filled=8,
        edge_orders=4,
    )
    base.update(kw)
    return BacktestResult(**base)  # type: ignore[arg-type]


def test_max_drawdown() -> None:
    eq = [(0, 100), (1, 120), (2, 90), (3, 130), (4, 125)]
    dd_usd, dd_pct = max_drawdown([(t, v * USD) for t, v in eq])
    assert dd_usd == 30 and dd_pct == pytest.approx(0.25)
    assert max_drawdown([]) == (0.0, 0.0)


def test_sharpe() -> None:
    assert sharpe([(0, 1), (1, 1)]) == 0.0
    flat = [(i * DAY, 1000 * USD) for i in range(5)]
    assert sharpe(flat) == 0.0
    up = [(0, 1000 * USD), (DAY, 1010 * USD), (2 * DAY, 1030 * USD), (3 * DAY, 1040 * USD)]
    assert sharpe(up) > 0
    assert sharpe(list(reversed([(t, v) for t, v in up]))) == 0.0  # span <= 0


def test_annualized_on_locked() -> None:
    # $100 on $500 locked for 2 days -> 20% per 2 days -> 36.5x a year
    assert annualized_on_locked(result()) == pytest.approx(0.2 * 365 / 2)
    assert annualized_on_locked(result(closed=[])) == 0.0


def test_compute_metrics() -> None:
    m = compute(result())
    assert m.net_pnl_usd == 100 and m.return_on_capital == pytest.approx(0.1)
    assert m.hit_rate == 1.0 and m.closed_positions == 1
    assert m.fill_rate == 0.8 and m.leg_failure_rate == 0.25
    assert m.theoretical_edge_usd == 120 and m.slippage_usd == 20
    assert m.avg_edge_per_entry_usd == 30 and m.avg_pnl_per_position_usd == 100
    assert m.unwind_cost_usd == 5 and m.label == "synthetic"
    empty = compute(result(closed=[], orders=0, legs=None, edge_orders=0, equity=[]))
    assert empty.hit_rate == 0 and empty.fill_rate == 0 and empty.leg_failure_rate == 0


def test_edge_table_capture() -> None:
    rows = edge_table([result(), result(expected_edge=0)])
    assert rows[0].capture == pytest.approx(100 / 120)
    assert rows[1].capture == 0.0


def test_report_html(tmp_path: Path) -> None:
    fill = Fill(1, "tok<script>", "BUY", 5000, 10**6, 5, "taker", 0, 0)
    res = result(
        fills=[fill],
        decisions=[Decision(1, "reject", "insufficient cash: <x>"), Decision(2, "order", "ok")],
    )
    html = render(res)
    assert "SYNTHETIC DATA" in html and "data:image/png;base64," in html
    assert "tok&lt;script&gt;" in html and "<script>" not in html
    assert "insufficient cash: &lt;x&gt;" in html and "1 leg groups failed" in html
    out = write_report(result(label="recorded", legs=None), tmp_path / "r" / "x.html")
    text = out.read_text()
    assert "Recorded market data." in text and "No failures." in text


def test_report_truncates_long_trade_tables() -> None:
    fills = [Fill(i, "t", "BUY", 5000, 10**6, i, "taker", 0, 0) for i in range(510)]
    assert "10 more fills not shown" in render(result(fills=fills))


def test_metrics_on_real_backtest() -> None:
    res = run_full(RunSpec.make("complementary", {}, SyntheticConfig(duration_hours=1)))
    m = compute(res)
    assert m.label == "synthetic" and m.fill_rate > 0.9
    assert m.slippage_usd == pytest.approx(usd(res.expected_edge - res.net_pnl))
