"""Save and restore a portfolio as JSON so a paper session restarts cleanly.

Writes go to a temp file and are renamed into place, so a crash mid-write
leaves the previous state intact. Open simulated orders are not saved: a
restart behaves like cancel-on-disconnect.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pmsim.engine.portfolio import ClosedPosition, Portfolio, Position

STATE_VERSION = 1


def to_dict(pf: Portfolio) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "cash": pf.cash,
        "initial_cash": pf.initial_cash,
        "now": pf.now,
        "cash_ms": pf.cash_ms,
        "deployed_ms": pf.deployed_ms,
        "positions": {t: asdict(p) for t, p in pf.positions.items()},
        "closed": [asdict(c) for c in pf.closed],
    }


def from_dict(raw: dict[str, Any]) -> Portfolio:
    if raw.get("version") != STATE_VERSION:
        raise ValueError(f"unsupported state version {raw.get('version')!r}")
    pf = Portfolio(cash=int(raw["cash"]), now=int(raw["now"]))
    pf.initial_cash = int(raw["initial_cash"])
    pf.cash_ms = int(raw["cash_ms"])
    pf.deployed_ms = int(raw["deployed_ms"])
    pf.positions = {t: Position(**p) for t, p in raw["positions"].items()}
    pf.closed = [ClosedPosition(**c) for c in raw["closed"]]
    return pf


def save(pf: Portfolio, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(to_dict(pf)))
    os.replace(tmp, path)


def load(path: Path) -> Portfolio | None:
    if not path.exists():
        return None
    return from_dict(json.loads(path.read_text()))
