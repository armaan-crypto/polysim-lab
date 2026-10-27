"""Dashboard API as plain functions returning JSON-ready dicts.

The HTTP layer in `server.py` only routes requests here, so everything is
testable without a socket. Nothing here can reach a trading endpoint: runs
use the simulator, and paper sessions are read from their saved files.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
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
from pmsim.strategies import custom
from pmsim.strategies.registry import BUILDERS, CONFIGS, INFO

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
        spec: dict[str, Any] = {
            "name": name,
            "type": kind,
            "default": field.default,
            "title": field.title or name.replace("_", " ").capitalize(),
            "help": field.description or "",
        }
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


def algorithms() -> list[dict[str, Any]]:
    """Built-in algorithms, then the user's own from the `algorithms/` folder."""
    out = [
        {
            "name": name,
            "title": INFO[name].title,
            "blurb": INFO[name].blurb,
            "how": INFO[name].how,
            "category": INFO[name].category,
            "presets": INFO[name].presets,
            "params": param_fields(CONFIGS[name]),
            "custom": False,
            "error": "",
        }
        for name in sorted(BUILDERS)
    ]
    for algo in custom.discover():
        out.append(
            {
                "name": algo.name,
                "title": algo.title,
                "blurb": algo.blurb or f"Your algorithm in {algo.path}.",
                "how": f"Loaded from {algo.path}. Edit the file and reload this page.",
                "category": "Your algorithm",
                "presets": {},
                "params": param_fields(algo.config) if algo.config else [],
                "custom": True,
                "error": algo.error,
            }
        )
    return out


def meta(cfg: Config) -> dict[str, Any]:
    return {
        "strategies": algorithms(),
        "algorithms_dir": str(custom.ALGORITHMS_DIR.resolve()),
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
        tokens = body.get("tokens") or []
        if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
            raise ApiError("tokens must be a list of token ids")
        return RecordedSpec(str(cfg.data_dir), tuple(sorted(tokens)))
    if kind != "synthetic":
        raise ApiError("data must be 'synthetic' or 'recorded'")
    hours = _num(body, "hours", 6, 0.25, MAX_HOURS)
    seed = int(_num(body, "seed", cfg.seed, 0, 2**31))
    return SyntheticConfig(duration_hours=hours, seed=seed)


def _config_model(name: str) -> type[BaseModel] | None:
    if name in CONFIGS:
        return CONFIGS[name]
    for algo in custom.discover():
        if algo.name == name:
            if algo.error:
                raise ApiError(f"{algo.path} failed to load: {algo.error}")
            return algo.config
    raise ApiError(f"unknown strategy {name!r}")


def strategy_params(name: str, raw: object) -> dict[str, Any]:
    model = _config_model(name)
    if not isinstance(raw, Mapping):
        raise ApiError("params must be an object")
    params = {k: v for k, v in raw.items() if v is not None and v != ""}
    if model is None:
        return {}
    try:
        model.model_validate(params)
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


SYNTHETIC_NAMES = (
    (re.compile(r"^syn(\d+)-(yes|no)$"), "Simulated market {0} / {1}"),
    (re.compile(r"^grp(\d+)-(\d+)-(yes|no)$"), "Simulated group {0}, outcome {1} / {2}"),
    (re.compile(r"^nest(\d+)-(sup|sub)-(yes|no)$"), "Simulated pair {0}, {1} / {2}"),
    (re.compile(r"^kalshi:K-syn(\d+):(yes|no)$"), "Kalshi twin of market {0} / {1}"),
)
WORDS = {"yes": "YES", "no": "NO", "sup": "superset", "sub": "subset"}


def _label(token: str, labels: Mapping[str, str]) -> str:
    """Readable token name: catalog slug for recorded data, plain words for synthetic."""
    if token in labels:
        return labels[token]
    for pattern, template in SYNTHETIC_NAMES:
        m = pattern.match(token)
        if m:
            return template.format(*(WORDS.get(p, p) for p in m.groups()))
    return token


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
    issues = [d for d in res.decisions if d.kind in ("reject", "resolution")]
    failures = [
        {"ts": d.ts, "kind": d.kind, "reason": d.reason, "detail": _relabel(d.detail, labels)}
        for d in issues[:MAX_ROWS]
    ]
    issue_counts = Counter(f"{d.kind}: {d.reason}" for d in issues)
    legs = res.legs
    return {
        "strategy": res.strategy,
        "label": res.label,
        "metrics": m.as_dict(),
        "equity": _downsample(res.equity),
        "start_cash": res.start_cash / CASH_SCALE,
        "t0": res.equity[0][0] if res.equity else 0,
        "fills": fills,
        "fill_count": len(res.fills),
        "failures": failures,
        "issue_counts": issue_counts.most_common(),
        "legs": None
        if legs is None
        else {"groups": legs.groups, "complete": legs.complete, "failed": legs.failed},
        "rejects": dict(res.rejects),
        "orders": res.orders,
    }


def _relabel(text: str, labels: Mapping[str, str]) -> str:
    """Swap token ids inside a log line for readable names."""
    return " ".join(_label(w, labels) for w in text.split(" "))


def summary(payload: Mapping[str, Any]) -> str:
    """One plain-English sentence describing the run."""
    m = payload["metrics"]
    data = payload["data"]
    windows = {"1d": "day", "1w": "week", "1m": "month", "max": "full history"}
    if data["kind"] == "synthetic":
        where = f"{data['hours']:g} simulated hours"
    elif data["kind"] == "historical":
        where = f"the last {windows.get(data['window'], data['window'])} of real prices"
    else:
        where = "the recorded order books"
    pnl = m["net_pnl_usd"]
    if payload["fill_count"] == 0:
        text = f"No trades over {where}: the algorithm never found an entry it liked."
    else:
        verb = "Made" if pnl > 0 else "Lost" if pnl < 0 else "Broke even with"
        amount = f" ${abs(pnl):,.2f} ({m['return_on_capital'] * 100:+.2f}%)" if pnl else ""
        text = f"{verb}{amount} over {where} from {payload['fill_count']} fills."
    stress = payload.get("stress")
    if stress:
        text += (
            " Stayed profitable under every stress test."
            if stress["passed"]
            else " Did not stay profitable under every stress test."
        )
    if m["theoretical_edge_usd"]:
        share = m["net_pnl_usd"] / m["theoretical_edge_usd"]
        text += f" Realized {share:.0%} of its expected edge."
    return text


def run(cfg: Config, body: Mapping[str, Any]) -> dict[str, Any]:
    name = str(body.get("strategy", ""))
    params = strategy_params(name, body.get("params", {}))
    data = data_spec(cfg, body)
    bt = BacktestConfig(latency_ms=cfg.latency_ms, gas_per_trade_usd=cfg.fees.gas_per_trade_usd)
    labels = token_labels(str(cfg.data_dir)) if isinstance(data, RecordedSpec) else {}
    started = time.perf_counter()
    try:
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
            payload["stress"] = {"rows": rows, "passed": all(r.net_pnl > 0 for _, r in results)}
    except Exception as exc:
        if name in BUILDERS:
            raise
        raise ApiError(f"your algorithm raised {type(exc).__name__}: {exc}") from exc
    payload["params"] = params
    payload["data"] = _describe(data)
    payload["elapsed_s"] = round(time.perf_counter() - started, 2)
    payload["summary"] = summary(payload)
    return payload


def _describe(data: DataSpec) -> dict[str, Any]:
    if isinstance(data, SyntheticConfig):
        return {"kind": "synthetic", "hours": data.duration_hours, "seed": data.seed}
    return {"kind": "recorded", "tokens": len(data.tokens)}


def compare(cfg: Config, body: Mapping[str, Any]) -> dict[str, Any]:
    data = data_spec(cfg, body)
    bt = BacktestConfig(latency_ms=cfg.latency_ms, gas_per_trade_usd=cfg.fees.gas_per_trade_usd)
    started = time.perf_counter()
    names = sorted(BUILDERS) + [a.name for a in custom.discover() if not a.error]
    results = [run_full(RunSpec.make(n, {}, data, backtest=bt)) for n in names]
    titles = {a["name"]: a["title"] for a in algorithms()}
    rows = []
    for res, edge in zip(results, edge_table(results), strict=True):
        m = compute(res)
        rows.append(
            {
                "strategy": res.strategy,
                "title": titles.get(res.strategy, res.strategy),
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


# ---- markets (Polymarket-style events) -----------------------------------------------


def _event_cards(df: pl.DataFrame, recorded: set[str]) -> list[dict[str, Any]]:
    cards = []
    for (event_id,), grp in df.group_by(["event_key"], maintain_order=True):
        yes = grp.filter(pl.col("outcome_index") == 0)
        no = {
            r["market_id"]: r
            for r in grp.filter(pl.col("outcome_index") == 1).iter_rows(named=True)
        }
        first = yes.row(0, named=True)
        markets = []
        for r in yes.sort("price", descending=True, nulls_last=True).iter_rows(named=True):
            other = no.get(r["market_id"], {})
            markets.append(
                {
                    "market_id": r["market_id"],
                    "label": r["group_item_title"] or r["question"],
                    "question": r["question"],
                    "outcomes": [r["outcome"], other.get("outcome", "No")],
                    "yes_price": r["price"],
                    "no_price": other.get("price"),
                    "yes_token": r["token_id"],
                    "no_token": other.get("token_id"),
                    "recorded": r["token_id"] in recorded or other.get("token_id") in recorded,
                    "status": r["status"],
                }
            )
        cards.append(
            {
                "event_id": str(event_id),
                "title": first["event_title"] or first["question"],
                "icon": first["icon"],
                "volume_24h": float(yes["volume_24h"].sum()),
                "volume": float(yes["volume"].sum()),
                "end": first["end_date"].isoformat() if first["end_date"] else None,
                "tags": [t for t in (first["tags"] or []) if t][:3],
                "neg_risk": bool(first["neg_risk"]),
                "fee_rate": first["fee_rate"],
                "markets": markets,
            }
        )
    return cards


def events(cfg: Config, query: str = "", limit: int = 60) -> dict[str, Any]:
    """Catalog grouped by event, biggest 24 h volume first. Prices are Gamma's at fetch time."""
    path = cfg.data_dir / "catalog.parquet"
    if not path.exists():
        return {"events": [], "total": 0, "fetched": None}
    df = pl.read_parquet(path).with_columns(
        pl.when(pl.col("event_id") != "")
        .then(pl.col("event_id"))
        .otherwise(pl.col("market_id"))
        .alias("event_key")
    )
    if query:
        q = query.lower()
        df = df.filter(
            pl.col("question").str.to_lowercase().str.contains(q, literal=True)
            | pl.col("event_title").str.to_lowercase().str.contains(q, literal=True)
            | pl.col("tags").list.contains(q)
        )
    order = (
        df.group_by("event_key")
        .agg(pl.col("volume_24h").sum().alias("v"))
        .sort("v", descending=True)
    )
    total = order.height
    keys = order["event_key"].head(min(limit, 200)).to_list()
    picked = (
        df.filter(pl.col("event_key").is_in(keys))
        .join(order.select("event_key", "v"), on="event_key")
        .sort("v", descending=True, maintain_order=True)
    )
    recorded = set(_recorded_books(str(cfg.data_dir)))
    return {
        "events": _event_cards(picked, recorded),
        "total": total,
        "fetched": int(path.stat().st_mtime * 1000),
    }


def _raw_signature(data_dir: Path) -> tuple[int, int]:
    files = list((data_dir / "raw").glob("*/*.parquet"))
    return len(files), max((int(f.stat().st_mtime) for f in files), default=0)


@lru_cache(maxsize=2)
def _books_at(data_dir: str, signature: tuple[int, int]) -> dict[str, dict[str, Any]]:
    from pmsim.book.replay import TradePrint, load_events, rebuild_books

    raw = Path(data_dir) / "raw"
    if not raw.exists():
        return {}
    events = load_events(raw)
    books = rebuild_books(events)
    last: dict[str, tuple[int, int]] = {}
    times: dict[str, int] = {}
    for ev in events:
        times[ev.token] = ev.ts
        if isinstance(ev, TradePrint):
            last[ev.token] = (ev.price, ev.size)
    return {
        token: {
            "bids": book.levels("BUY"),
            "asks": book.levels("SELL"),
            "ts": times.get(token, 0),
            "last": last.get(token),
        }
        for token, book in books.items()
    }


def _recorded_books(data_dir: str) -> dict[str, dict[str, Any]]:
    return _books_at(data_dir, _raw_signature(Path(data_dir)))


def book(cfg: Config, token: str, depth: int = 12) -> dict[str, Any]:
    """The recorded order book for one token, as of the end of the recording."""
    books = _recorded_books(str(cfg.data_dir))
    if token not in books:
        raise ApiError("no recorded book for this market; record it with `pmsim record`")
    b = books[token]

    def levels(rows: list[tuple[int, int]]) -> list[dict[str, float]]:
        out, total = [], 0.0
        for price, size in rows[:depth]:
            shares = size / SIZE_SCALE
            total += shares * price / PRICE_SCALE
            out.append({"price": price / PRICE_SCALE, "shares": shares, "total": total})
        return out

    bids, asks = levels(b["bids"]), levels(b["asks"])
    spread = asks[0]["price"] - bids[0]["price"] if bids and asks else None
    last = b["last"]
    return {
        "token": token,
        "label": _label(token, token_labels(str(cfg.data_dir))),
        "bids": bids,
        "asks": asks,
        "spread": spread,
        "last": None
        if last is None
        else {"price": last[0] / PRICE_SCALE, "shares": last[1] / SIZE_SCALE},
        "as_of": b["ts"],
    }
