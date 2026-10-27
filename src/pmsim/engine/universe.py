"""What a backtest runs over: token metadata, rules, groups, resolutions and events."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

import polars as pl

from pmsim.book.replay import BookDelta, BookSnapshot, MarketEvent
from pmsim.config import FeeConfig
from pmsim.data.kalshi import QuoteFeed
from pmsim.engine.fees import FeeModel, MarketRules, rules_from_catalog
from pmsim.risk.negrisk import NegRiskGroup, groups_from_catalog
from pmsim.risk.resolution import Resolution
from pmsim.strategies.base import TokenMeta, end_ts_ms

DataLabel = Literal["recorded", "synthetic"]


@dataclass(frozen=True)
class CrossLink:
    """A Polymarket binary market and its Kalshi twin."""

    name: str
    poly_yes: str
    poly_no: str
    kalshi_ticker: str
    mismatch: float


@dataclass(frozen=True)
class LogicalLink:
    """Event `subset` implies event `superset`."""

    name: str
    subset_no: str
    superset_yes: str


@dataclass
class Universe:
    meta: dict[str, TokenMeta]
    rules: dict[str, MarketRules]
    label: DataLabel
    groups: list[NegRiskGroup] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    feed: QuoteFeed | None = None
    cross_links: list[CrossLink] = field(default_factory=list)
    logical_links: list[LogicalLink] = field(default_factory=list)


@dataclass
class MarketData:
    universe: Universe
    events: list[MarketEvent]

    def window(self, start: int, end: int) -> MarketData:
        """Events with start <= ts < end; resolutions inside the window are kept."""
        events = [e for e in self.events if start <= e.ts < end]
        uni = replace(
            self.universe, resolutions=[r for r in self.universe.resolutions if r.ts < end]
        )
        return MarketData(uni, events)

    def span(self) -> tuple[int, int]:
        return (self.events[0].ts, self.events[-1].ts + 1) if self.events else (0, 0)


def scale_depth(events: list[MarketEvent], factor: float) -> list[MarketEvent]:
    """Stress helper: multiply every book size by `factor` (trades unchanged)."""
    out: list[MarketEvent] = []
    for ev in events:
        if isinstance(ev, BookSnapshot):
            bids = tuple((p, int(s * factor)) for p, s in ev.bids if int(s * factor) > 0)
            asks = tuple((p, int(s * factor)) for p, s in ev.asks if int(s * factor) > 0)
            out.append(replace(ev, bids=bids, asks=asks))
        elif isinstance(ev, BookDelta):
            out.append(replace(ev, size=int(ev.size * factor)))
        else:
            out.append(ev)
    return out


def scale_fees(rules: dict[str, MarketRules], factor: float) -> dict[str, MarketRules]:
    return {
        t: replace(
            r, fees=FeeModel(r.fees.taker_rate, r.fees.maker_rate, r.fees.multiplier * factor)
        )
        for t, r in rules.items()
    }


def universe_from_catalog(catalog: pl.DataFrame, fees: FeeConfig) -> Universe:
    """Universe for recorded data. Recorded markets are open, so there are no resolutions."""
    meta: dict[str, TokenMeta] = {}
    by_market: dict[str, list[dict[str, object]]] = {}
    for row in catalog.iter_rows(named=True):
        by_market.setdefault(row["market_id"], []).append(row)
    for rows in by_market.values():
        tokens = {int(str(r["outcome_index"])): str(r["token_id"]) for r in rows}
        for r in rows:
            idx = int(str(r["outcome_index"]))
            meta[str(r["token_id"])] = TokenMeta(
                str(r["token_id"]),
                str(r["market_id"]),
                idx,
                tokens.get(1 - idx, ""),
                end_ts_ms(r["end_date"]),  # type: ignore[arg-type]
                tuple(r["tags"] or ()),  # type: ignore[arg-type]
                str(r["rules"]),
                str(r["neg_risk_market_id"]) if r["neg_risk"] else "",
            )
    return Universe(
        meta, rules_from_catalog(catalog, fees), "recorded", groups_from_catalog(catalog)
    )
