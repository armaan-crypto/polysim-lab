"""Experiment runner: single runs, parameter sweeps, walk-forward splits, stress tests.

Runs are described by picklable `RunSpec`s so they can go to a process pool.
Results are returned in spec order, so parallel and serial runs agree.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from pmsim.book.replay import load_events
from pmsim.config import FeeConfig
from pmsim.data.synthetic import SyntheticConfig, generate
from pmsim.engine.backtest import Backtest, BacktestConfig, BacktestResult
from pmsim.engine.universe import MarketData, scale_depth, scale_fees, universe_from_catalog
from pmsim.strategies.registry import build_strategy

STRESSES = ("base", "latency_x3", "depth_x0.5", "fees_x2")


@dataclass(frozen=True)
class RecordedSpec:
    data_dir: str
    tokens: tuple[str, ...] = ()


DataSpec = SyntheticConfig | RecordedSpec


@dataclass(frozen=True)
class RunSpec:
    strategy: str
    params: tuple[tuple[str, Any], ...]
    data: DataSpec
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    stress: str = "base"
    window: tuple[int, int] | None = None

    @staticmethod
    def make(strategy: str, params: Mapping[str, Any], data: DataSpec, **kw: Any) -> RunSpec:
        return RunSpec(strategy, tuple(sorted(params.items())), data, **kw)


@dataclass(frozen=True)
class RunSummary:
    strategy: str
    params: tuple[tuple[str, Any], ...]
    stress: str
    label: str
    net_pnl_usd: float
    fills: int
    expected_edge_usd: float
    leg_failures: int
    unwind_cost_usd: float
    rejects: int


@lru_cache(maxsize=8)
def load_data(spec: DataSpec) -> MarketData:
    if isinstance(spec, SyntheticConfig):
        return generate(spec)
    root = Path(spec.data_dir)
    catalog = pl.read_parquet(root / "catalog.parquet")
    if spec.tokens:
        catalog = catalog.filter(pl.col("token_id").is_in(spec.tokens))
    tokens = set(catalog["token_id"].to_list())
    return MarketData(
        universe_from_catalog(catalog, FeeConfig()), load_events(root / "raw", tokens)
    )


def apply_stress(
    data: MarketData, cfg: BacktestConfig, stress: str
) -> tuple[MarketData, BacktestConfig]:
    if stress == "base":
        return data, cfg
    if stress == "latency_x3":
        return data, cfg.model_copy(update={"latency_ms": cfg.latency_ms * 3})
    if stress == "depth_x0.5":
        return MarketData(data.universe, scale_depth(data.events, 0.5)), cfg
    if stress == "fees_x2":
        uni = replace(data.universe, rules=scale_fees(data.universe.rules, 2.0))
        return MarketData(uni, data.events), cfg
    raise ValueError(f"unknown stress {stress!r}")


def run_full(spec: RunSpec) -> BacktestResult:
    data = load_data(spec.data)
    if spec.window is not None:
        data = data.window(*spec.window)
    data, cfg = apply_stress(data, spec.backtest, spec.stress)
    strategy = build_strategy(spec.strategy, dict(spec.params), data.universe)
    return Backtest(strategy, data, cfg).run()


def summarize(spec: RunSpec, res: BacktestResult) -> RunSummary:
    return RunSummary(
        spec.strategy,
        spec.params,
        spec.stress,
        res.label,
        res.net_pnl_usd(),
        len(res.fills),
        res.expected_edge / 10**10,
        res.legs.failed if res.legs else 0,
        res.unwind_cost / 10**10,
        sum(res.rejects.values()),
    )


def run_one(spec: RunSpec) -> RunSummary:
    return summarize(spec, run_full(spec))


def run_many(specs: Sequence[RunSpec], workers: int = 1) -> list[RunSummary]:
    if workers <= 1 or len(specs) <= 1:
        return [run_one(s) for s in specs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run_one, specs))


def param_grid(grid: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    keys = sorted(grid)
    return [
        dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(grid[k] for k in keys))
    ]


def sweep(
    strategy: str,
    grid: Mapping[str, Sequence[Any]],
    data: DataSpec,
    cfg: BacktestConfig | None = None,
    workers: int = 1,
) -> list[RunSummary]:
    cfg = cfg or BacktestConfig()
    specs = [RunSpec.make(strategy, p, data, backtest=cfg) for p in param_grid(grid)]
    return run_many(specs, workers)


@dataclass(frozen=True)
class Fold:
    train: tuple[int, int]
    test: tuple[int, int]
    best_params: tuple[tuple[str, Any], ...]
    train_pnl_usd: float
    test_pnl_usd: float


def walk_forward(
    strategy: str,
    grid: Mapping[str, Sequence[Any]],
    data: DataSpec,
    folds: int = 3,
    cfg: BacktestConfig | None = None,
    workers: int = 1,
) -> list[Fold]:
    """Split time into folds + 1 windows; tune on window k, test on window k + 1."""
    cfg = cfg or BacktestConfig()
    start, end = load_data(data).span()
    edges = [start + (end - start) * i // (folds + 1) for i in range(folds + 2)]
    out = []
    for k in range(folds):
        train, test = (edges[k], edges[k + 1]), (edges[k + 1], edges[k + 2])
        specs = [
            RunSpec.make(strategy, p, data, backtest=cfg, window=train) for p in param_grid(grid)
        ]
        results = run_many(specs, workers)
        best = max(results, key=lambda r: r.net_pnl_usd)
        tested = run_one(RunSpec(strategy, best.params, data, cfg, window=test))
        out.append(Fold(train, test, best.params, best.net_pnl_usd, tested.net_pnl_usd))
    return out


@dataclass(frozen=True)
class StressReport:
    results: tuple[RunSummary, ...]

    @property
    def passed(self) -> bool:
        """Positive net PnL in the base run and under every single mild stress."""
        return all(r.net_pnl_usd > 0 for r in self.results)

    @property
    def label(self) -> str:
        return self.results[0].label if self.results else ""


def stress_test(
    strategy: str,
    params: Mapping[str, Any],
    data: DataSpec,
    cfg: BacktestConfig | None = None,
    workers: int = 1,
) -> StressReport:
    cfg = cfg or BacktestConfig()
    specs = [RunSpec.make(strategy, params, data, backtest=cfg, stress=s) for s in STRESSES]
    return StressReport(tuple(run_many(specs, workers)))
