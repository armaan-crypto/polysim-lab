"""Build reference strategies by name from plain parameter dicts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from pmsim.data.kalshi import MockQuoteFeed
from pmsim.engine.universe import Universe
from pmsim.strategies.base import Strategy
from pmsim.strategies.complementary import ComplementaryArb, ComplementaryConfig
from pmsim.strategies.cross_platform import CrossPair, CrossPlatformConfig, CrossPlatformSpread
from pmsim.strategies.logical import LogicalArb, LogicalConfig, Relation
from pmsim.strategies.market_making import MarketMaker, MarketMakingConfig
from pmsim.strategies.near_resolution import NearResolutionConfig, NearResolutionYield
from pmsim.strategies.negrisk_sum import NegRiskSumArb, NegRiskSumConfig

Params = Mapping[str, Any]


def _cross(params: Params, uni: Universe) -> Strategy:
    cfg = dict(params)
    if "pairs" not in cfg:
        cfg["pairs"] = [
            CrossPair(
                name=c.name,
                poly_yes=c.poly_yes,
                poly_no=c.poly_no,
                kalshi_ticker=c.kalshi_ticker,
                mismatch=c.mismatch,
            )
            for c in uni.cross_links
        ]
    feed = uni.feed or MockQuoteFeed({})
    return CrossPlatformSpread(feed, CrossPlatformConfig.model_validate(cfg))


def _logical(params: Params, uni: Universe) -> Strategy:
    cfg = dict(params)
    if "relations" not in cfg:
        cfg["relations"] = [
            Relation(name=r.name, subset_no=r.subset_no, superset_yes=r.superset_yes)
            for r in uni.logical_links
        ]
    return LogicalArb(LogicalConfig.model_validate(cfg))


BUILDERS: dict[str, Callable[[Params, Universe], Strategy]] = {
    "complementary": lambda p, _: ComplementaryArb(ComplementaryConfig.model_validate(p)),
    "negrisk_sum": lambda p, _: NegRiskSumArb(NegRiskSumConfig.model_validate(p)),
    "near_resolution": lambda p, _: NearResolutionYield(NearResolutionConfig.model_validate(p)),
    "logical": _logical,
    "market_making": lambda p, _: MarketMaker(MarketMakingConfig.model_validate(p)),
    "cross_platform": _cross,
}


CONFIGS: dict[str, type[BaseModel]] = {
    "complementary": ComplementaryConfig,
    "negrisk_sum": NegRiskSumConfig,
    "near_resolution": NearResolutionConfig,
    "logical": LogicalConfig,
    "market_making": MarketMakingConfig,
    "cross_platform": CrossPlatformConfig,
}

DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "complementary": (
        "Complementary arbitrage",
        "Buys YES and NO of one market when the pair costs under $1 after fees and gas.",
    ),
    "negrisk_sum": (
        "Neg-risk sum arbitrage",
        "Buys every YES (or every NO) in a mutually exclusive group when the set is mispriced.",
    ),
    "near_resolution": (
        "Near-resolution yield",
        "Buys 0.95 to 0.99 outcomes close to the end date when the rules text looks clean.",
    ),
    "logical": (
        "Logical arbitrage",
        "Buys NO on a subset event and YES on its superset when prices break the implication.",
    ),
    "market_making": (
        "Market making",
        "Quotes both sides around the mid, skewed against inventory, post only.",
    ),
    "cross_platform": (
        "Cross-platform spread",
        "Pairs a Polymarket leg with the opposite Kalshi leg when the two cost under $1.",
    ),
}


def build_strategy(name: str, params: Params, universe: Universe) -> Strategy:
    if name not in BUILDERS:
        raise KeyError(f"unknown strategy {name!r}; known: {sorted(BUILDERS)}")
    return BUILDERS[name](params, universe)
