"""Dashboard API as plain functions returning JSON-ready dicts.

The HTTP layer in `server.py` only routes requests here, so everything is
testable without a socket. Nothing here can reach a trading endpoint: runs
use the simulator, and paper sessions are read from their saved files.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel, ValidationError

from pmsim.analysis.metrics import compute, edge_table
from pmsim.book.units import CASH_SCALE, PRICE_SCALE, SIZE_SCALE
from pmsim.config import Config
from pmsim.data.synthetic import SyntheticConfig
from pmsim.engine.backtest import BacktestConfig, BacktestResult
from pmsim.engine.experiment import STRESSES, DataSpec, RecordedSpec, RunSpec, run_full
from pmsim.strategies.registry import BUILDERS, CONFIGS, DESCRIPTIONS

MAX_HOURS = 72.0
MAX_POINTS = 400
MAX_ROWS = 300
SIMPLE_TYPES = {int: "int", float: "float", bool: "bool"}


class ApiError(ValueError):
    """A bad request; the message is shown to the user."""


# ---- metadata -----------------------------------------------------------------


def param_fields(model: type[BaseModel]) -> list[dict[str, Any]]:
    """Scalar config fields with defaults and bounds, for building a form."""
    out = []
    for name, field in model.model_fields.items():
        kind = SIMPLE_TYPES.get(field.annotation)  # type: ignore[arg-type]
        if kind is None:
            continue
        spec: dict[str, Any] = {"name": name, "type": kind, "default": field.default}
        for m in field.metadata:
            for bound in ("ge", "gt", "le", "lt"):
                if getattr(m, bound, None) is not None:
                    spec[bound] = getattr(m, bound)
        out.append(spec)
    return out


def recorded_summary(data_dir: Path) -> dict[str, Any]:
    catalog = data_dir / "catalog.parquet"
    raw = data_dir / "raw"
    tables = {}
    if raw.exists():
        for table in sorted(p.name for p in raw.iterdir() if p.is_dir()):
            files = list((raw / table).glob("*.parquet"))
            if files:
                tables[table] = pl.scan_parquet(files).select(pl.len()).collect().item()
    markets = 0
    if catalog.exists():
        markets = pl.scan_parquet(catalog).select(pl.col("market_id").n_unique()).collect().item()
    return {"available": bool(markets and tables), "markets": markets, "rows": tables}


def meta(cfg: Config) -> dict[str, Any]:
    strategies = [
        {
            "name": name,
            "title": DESCRIPTIONS[name][0],
            "blurb": DESCRIPTIONS[name][1],
            "params": param_fields(CONFIGS[name]),
        }
        for name in sorted(BUILDERS)
    ]
    return {
        "strategies": strategies,
        "stresses": list(STRESSES),
        "recorded": recorded_summary(cfg.data_dir),
        "defaults": {"hours": 6, "seed": cfg.seed, "latency_ms": cfg.latency_ms},
    }


# ---- runs -------------------------------------------------------------------------


def _num(body: Mapping[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(body.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{key} must be a number") from exc
    if not lo <= value <= hi:
        raise ApiError(f"{key} must be between {lo:g} and {hi:g}")
    return value


def data_spec(cfg: Config, body: Mapping[str, Any]) -> DataSpec:
    kind = body.get("data", "synthetic")
    if kind == "recorded":
        if not recorded_summary(cfg.data_dir)["available"]:
            raise ApiError("no recorded data: run `pmsim fetch` and `pmsim record` first")
        return RecordedSpec(str(cfg.data_dir))
    if kind != "synthetic":
        raise ApiError("data must be 'synthetic' or 'recorded'")
    hours = _num(body, "hours", 6, 0.25, MAX_HOURS)
    seed = int(_num(body, "seed", cfg.seed, 0, 2**31))
    return SyntheticConfig(duration_hours=hours, seed=seed)


def strategy_params(name: str, raw: object) -> dict[str, Any]:
    if name not in BUILDERS:
        raise ApiError(f"unknown strategy {name!r}")
    if not isinstance(raw, Mapping):
        raise ApiError("params must be an object")
    params = {k: v for k, v in raw.items() if v is not None and v != ""}
    try:
        CONFIGS[name].model_validate(params)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"])
        raise ApiError(f"{field}: {first['msg']}") from exc
    return params


@lru_cache(maxsize=4)
def token_labels(data_dir: str) -> dict[str, str]:
    """Readable names for recorded tokens: market slug plus outcome."""
    path = Path(data_dir) / "catalog.parquet"
    if not path.exists():
        return {}
    df = pl.read_parquet(path, columns=["token_id", "slug", "outcome"])
    return {t: f"{slug} / {out}" for t, slug, out in df.iter_rows()}


def _label(token: str, labels: Mapping[str, str]) -> str:
    return labels.get(token, token)


def _downsample(points: list[tuple[int, int]]) -> list[list[float]]:
    if not points:
        return []
    step = max(1, len(points) // MAX_POINTS)
    picked = points[::step]
    if picked[-1] != points[-1]:
        picked.append(points[-1])
    t0 = points[0][0]
    return [[(t - t0) / 3_600_000, v / CASH_SCALE] for t, v in picked]


def result_payload(res: BacktestResult, labels: Mapping[str, str]) -> dict[str, Any]:
    m = compute(res)
    fills = [
        {
            "ts": f.ts,
            "token": _label(f.token, labels),
            "side": f.side,
            "price": f.price / PRICE_SCALE,
            "size": f.size / SIZE_SCALE,
            "liquidity": f.liquidity,
            "fee": f.fee / CASH_SCALE,
        }
        for f in res.fills[:MAX_ROWS]
    ]
    failures = [
        {"ts": d.ts, "kind": d.kind, "reason": d.reason, "detail": d.detail}
        for d in res.decisions
        if d.kind in ("reject", "resolution")
    ][:MAX_ROWS]
    legs = res.legs
    return {
        "strategy": res.strategy,
        "label": res.label,
        "metrics": m.as_dict(),
        "equity": _downsample(res.equity),
        "start_cash": res.start_cash / CASH_SCALE,
        "fills": fills,
        "fill_count": len(res.fills),
        "failures": failures,
        "legs": None
        if legs is None
        else {"groups": legs.groups, "complete": legs.complete, "failed": legs.failed},
        "rejects": dict(res.rejects),
        "orders": res.orders,
    }


def run(cfg: Config, body: Mapping[str, Any]) -> dict[str, Any]:
    name = str(body.get("strategy", ""))
    params = strategy_params(name, body.get("params", {}))
    data = data_spec(cfg, body)
    bt = BacktestConfig(latency_ms=cfg.latency_ms, gas_per_trade_usd=cfg.fees.gas_per_trade_usd)
    labels = token_labels(str(cfg.data_dir)) if isinstance(data, RecordedSpec) else {}
    started = time.perf_counter()
    base = run_full(RunSpec.make(name, params, data, backtest=bt))
    payload = result_payload(base, labels)
    payload["stress"] = None
    if body.get("stress"):
        results = [("base", base)] + [
            (s, run_full(RunSpec.make(name, params, data, backtest=bt, stress=s)))
            for s in STRESSES[1:]
        ]
        rows = [
            {"stress": s, "net_pnl": r.net_pnl_usd(), "fills": len(r.fills)} for s, r in results
        ]
        passed = all(r.net_pnl > 0 for _, r in results)
        payload["stress"] = {"rows": rows, "passed": passed}
    payload["params"] = params
    payload["data"] = _describe(data)
    payload["elapsed_s"] = round(time.perf_counter() - started, 2)
    return payload


def _describe(data: DataSpec) -> dict[str, Any]:
    if isinstance(data, SyntheticConfig):
        return {"kind": "synthetic", "hours": data.duration_hours, "seed": data.seed}
    return {"kind": "recorded"}


def compare(cfg: Config, body: Mapping[str, Any]) -> dict[str, Any]:
    data = data_spec(cfg, body)
    bt = BacktestConfig(latency_ms=cfg.latency_ms, gas_per_trade_usd=cfg.fees.gas_per_trade_usd)
    started = time.perf_counter()
    results = [run_full(RunSpec.make(n, {}, data, backtest=bt)) for n in sorted(BUILDERS)]
    rows = []
    for res, edge in zip(results, edge_table(results), strict=True):
        m = compute(res)
        rows.append(
            {
                "strategy": res.strategy,
                "title": DESCRIPTIONS[res.strategy][0],
                "label": res.label,
                "theoretical": edge.theoretical_usd,
                "realized": edge.realized_usd,
                "capture": edge.capture,
                "fills": len(res.fills),
                "sharpe": m.sharpe,
                "max_drawdown": m.max_drawdown_usd,
                "hit_rate": m.hit_rate,
            }
        )
    return {
        "rows": rows,
        "data": _describe(data),
        "elapsed_s": round(time.perf_counter() - started, 2),
    }


# ---- paper sessions ------------------------------------------------------------------


def _paper_dir(cfg: Config) -> Path:
    return cfg.data_dir / "paper"


def paper_sessions(cfg: Config) -> dict[str, Any]:
    root = _paper_dir(cfg)
    sessions = []
    for path in sorted(root.glob("*.state.json")) if root.exists() else []:
        name = path.name.removesuffix(".state.json")
        raw = json.loads(path.read_text())
        open_positions = sum(1 for p in raw["positions"].values() if p["size"])
        sessions.append(
            {
                "name": name,
                "updated": int(path.stat().st_mtime * 1000),
                "cash": raw["cash"] / CASH_SCALE,
                "initial_cash": raw["initial_cash"] / CASH_SCALE,
                "realized": sum(p["realized"] for p in raw["positions"].values()) / CASH_SCALE,
                "open_positions": open_positions,
            }
        )
    return {"sessions": sessions}


def paper_session(cfg: Config, name: str, tail: int = 100) -> dict[str, Any]:
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ApiError("bad session name")
    root = _paper_dir(cfg)
    state_path = root / f"{name}.state.json"
    if not state_path.exists():
        raise ApiError(f"no paper session {name!r}")
    raw = json.loads(state_path.read_text())
    labels = token_labels(str(cfg.data_dir))
    positions = [
        {
            "token": _label(token, labels),
            "shares": p["size"] / SIZE_SCALE,
            "avg_price": p["cost"] / p["size"] / PRICE_SCALE,
            "cost": p["cost"] / CASH_SCALE,
            "realized": p["realized"] / CASH_SCALE,
        }
        for token, p in raw["positions"].items()
        if p["size"]
    ]
    log_path = root / f"{name}.log.jsonl"
    lines = log_path.read_text().splitlines()[-tail:] if log_path.exists() else []
    decisions = [json.loads(line) for line in reversed(lines)]
    for d in decisions:
        d["detail"] = _shorten_tokens(d["detail"], labels)
    return {
        "name": name,
        "updated": int(state_path.stat().st_mtime * 1000),
        "cash": raw["cash"] / CASH_SCALE,
        "initial_cash": raw["initial_cash"] / CASH_SCALE,
        "realized": sum(p["realized"] for p in raw["positions"].values()) / CASH_SCALE,
        "positions": positions,
        "decisions": decisions,
        "closed": len(raw["closed"]),
    }


def _shorten_tokens(text: str, labels: Mapping[str, str]) -> str:
    """Swap long token ids in log lines for readable labels."""
    for token, label in labels.items():
        if token in text:
            text = text.replace(token, label)
    return text


# ---- catalog ---------------------------------------------------------------------------


def catalog(cfg: Config, query: str = "", limit: int = 100) -> dict[str, Any]:
    path = cfg.data_dir / "catalog.parquet"
    if not path.exists():
        return {"markets": [], "total": 0}
    df = pl.read_parquet(path).filter(pl.col("outcome_index") == 0)
    if query:
        q = query.lower()
        df = df.filter(
            pl.col("slug").str.contains(q, literal=True)
            | pl.col("question").str.to_lowercase().str.contains(q, literal=True)
            | pl.col("tags").list.contains(q)
        )
    total = df.height
    rows = [
        {
            "question": r["question"],
            "slug": r["slug"],
            "status": r["status"],
            "neg_risk": r["neg_risk"],
            "tick": r["tick_size"],
            "fee_rate": r["fee_rate"],
            "fee_type": r["fee_type"],
            "end": r["end_date"].isoformat() if r["end_date"] else None,
            "tags": list(r["tags"] or [])[:4],
        }
        for r in df.head(min(limit, 500)).iter_rows(named=True)
    ]
    return {"markets": rows, "total": total}
