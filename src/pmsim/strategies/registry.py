"""Build reference strategies by name from plain parameter dicts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pmsim.data.kalshi import MockQuoteFeed
from pmsim.engine.universe import Universe
from pmsim.strategies.base import Strategy
from pmsim.strategies.complementary import ComplementaryArb, ComplementaryConfig
from pmsim.strategies.cross_platform import CrossPair, CrossPlatformConfig, CrossPlatformSpread
from pmsim.strategies.logical import LogicalArb, LogicalConfig
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


BUILDERS: dict[str, Callable[[Params, Universe], Strategy]] = {
    "complementary": lambda p, _: ComplementaryArb(ComplementaryConfig.model_validate(p)),
    "negrisk_sum": lambda p, _: NegRiskSumArb(NegRiskSumConfig.model_validate(p)),
    "near_resolution": lambda p, _: NearResolutionYield(NearResolutionConfig.model_validate(p)),
    "logical": lambda p, _: LogicalArb(LogicalConfig.model_validate(p)),
    "market_making": lambda p, _: MarketMaker(MarketMakingConfig.model_validate(p)),
    "cross_platform": _cross,
}


def build_strategy(name: str, params: Params, universe: Universe) -> Strategy:
    if name not in BUILDERS:
        raise KeyError(f"unknown strategy {name!r}; known: {sorted(BUILDERS)}")
    return BUILDERS[name](params, universe)
