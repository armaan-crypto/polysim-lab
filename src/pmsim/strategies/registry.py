"""Build reference strategies by name from plain parameter dicts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from pmsim.data.kalshi import MockQuoteFeed
from pmsim.engine.universe import Universe
from pmsim.strategies import custom
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


@dataclass(frozen=True)
class Info:
    title: str
    blurb: str
    how: str
    category: str
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)


INFO: dict[str, Info] = {
    "complementary": Info(
        "Complementary arbitrage",
        "Buys YES and NO of one market when the pair costs under $1 after fees and gas.",
        "YES and NO of the same market always pay $1 together. When both can be bought for "
        "less, the difference is locked in. In recorded Polymarket books the two sides mirror "
        "each other, so expect this to find nothing on real data.",
        "Arbitrage",
        {
            "Cautious": {"min_edge_usd_per_share": 0.02, "max_shares": 50},
            "Aggressive": {"min_edge_usd_per_share": 0.001, "max_shares": 300},
        },
    ),
    "negrisk_sum": Info(
        "Neg-risk sum arbitrage",
        "Buys every YES (or every NO) in a mutually exclusive group when the set is mispriced.",
        "In a multi-outcome event exactly one outcome wins, so one YES of each pays exactly $1 "
        "and one NO of each pays $(n - 1). When a full set costs less than its payout, "
        "it buys the whole set.",
        "Arbitrage",
        {
            "Cautious": {"min_edge_usd_per_set": 0.03, "max_sets": 20},
            "Aggressive": {"min_edge_usd_per_set": 0.002, "max_sets": 200},
        },
    ),
    "near_resolution": Info(
        "Near-resolution yield",
        "Buys 0.95 to 0.99 outcomes close to the end date when the rules text looks clean.",
        "Buys outcomes that look almost certain, shortly before the market ends, and collects "
        "the last few cents at payout. Not arbitrage: one surprise loses most of the stake.",
        "Directional",
        {
            "Cautious": {"min_price": 0.97, "max_risk_score": 0.5, "max_shares": 100},
            "Aggressive": {"min_price": 0.9, "max_risk_score": 3.0, "max_shares": 500},
        },
    ),
    "logical": Info(
        "Logical arbitrage",
        "Buys NO on a subset event and YES on its superset when prices break the implication.",
        "If A implies B (win by 10 implies win), A can never be more likely than B. When the "
        "market prices it that way, NO on A plus YES on B pays at least $1 in every outcome.",
        "Arbitrage",
        {
            "Cautious": {"min_edge_usd_per_share": 0.03, "max_shares": 50},
            "Aggressive": {"min_edge_usd_per_share": 0.002, "max_shares": 300},
        },
    ),
    "market_making": Info(
        "Market making",
        "Quotes both sides around the mid, skewed against inventory, post only.",
        "Posts a bid and an offer around the mid price and earns the spread when both fill. "
        "Lowers its quotes as inventory builds up so it sells down risk.",
        "Market making",
        {
            "Cautious": {"half_spread_ticks": 4, "quote_shares": 20, "max_inventory_shares": 200},
            "Aggressive": {
                "half_spread_ticks": 1,
                "quote_shares": 150,
                "max_inventory_shares": 1500,
            },
        },
    ),
    "cross_platform": Info(
        "Cross-platform spread",
        "Pairs a Polymarket leg with the opposite Kalshi leg when the two cost under $1.",
        "Buys YES on one venue and NO on the other when together they cost under $1. It pays "
        "only if both venues resolve the same way, so pairs with different rules are penalized.",
        "Arbitrage",
        {
            "Cautious": {"min_edge_usd_per_share": 0.03, "max_mismatch": 0.15},
            "Aggressive": {"min_edge_usd_per_share": 0.002, "max_mismatch": 0.5},
        },
    ),
}


def build_strategy(name: str, params: Params, universe: Universe) -> Strategy:
    """A built-in algorithm by name, else one from the `algorithms/` folder."""
    if name in BUILDERS:
        return BUILDERS[name](params, universe)
    for algo in custom.discover():
        if algo.name == name:
            return custom.build(algo, dict(params))
    raise KeyError(f"unknown strategy {name!r}; known: {sorted(BUILDERS)}")
