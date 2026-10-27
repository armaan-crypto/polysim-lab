"""Fee model and per-token trading rules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl

from pmsim.book.units import PRICE_SCALE, SIZE_SCALE, size_units, tick_units, usd_to_cash
from pmsim.config import FeeConfig

Liquidity = Literal["taker", "maker"]


@dataclass(frozen=True)
class FeeModel:
    """Fee in USDC = C * rate * p * (1 - p), with C shares at price p."""

    taker_rate: float
    maker_rate: float = 0.0
    multiplier: float = 1.0

    def fee(self, price: int, size: int, liquidity: Liquidity) -> int:
        """Fee in cash units, rounded to whole micro-USDC."""
        rate = self.taker_rate if liquidity == "taker" else self.maker_rate
        p = price / PRICE_SCALE
        return usd_to_cash(size / SIZE_SCALE * rate * self.multiplier * p * (1 - p))


@dataclass(frozen=True)
class MarketRules:
    tick: int
    min_size: int
    fees: FeeModel


def fee_model(fee_type: str | None, market_rate: float, cfg: FeeConfig) -> FeeModel:
    """Use the market's schedule when it has one, else the configured default."""
    rate = cfg.default_taker_rate if fee_type is None else market_rate
    return FeeModel(rate, cfg.maker_rate, cfg.multiplier)


def rules_from_catalog(catalog: pl.DataFrame, cfg: FeeConfig) -> dict[str, MarketRules]:
    cols = ["token_id", "tick_size", "min_order_size", "fee_type", "fee_rate"]
    return {
        token: MarketRules(tick_units(tick), size_units(min_size), fee_model(ftype, rate, cfg))
        for token, tick, min_size, ftype, rate in catalog.select(cols).iter_rows()
    }
