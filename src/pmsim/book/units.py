"""Exact integer units for prices, sizes and cash.

Prices are in units of 1e-4 (the finest Polymarket tick), sizes in 1e-6
shares (USDC has 6 decimals). Cash is price * size, so 1 USDC is 1e10 cash
units and every fill is exact.
"""

from __future__ import annotations

from typing import Literal

PRICE_SCALE = 10_000
SIZE_SCALE = 1_000_000
CASH_SCALE = PRICE_SCALE * SIZE_SCALE

Side = Literal["BUY", "SELL"]


def price_units(price: float) -> int:
    return round(price * PRICE_SCALE)


def size_units(size: float) -> int:
    return round(size * SIZE_SCALE)


def tick_units(tick: float) -> int:
    units = price_units(tick)
    if units <= 0 or abs(units / PRICE_SCALE - tick) > 1e-12:
        raise ValueError(f"tick {tick} is not a multiple of 1/{PRICE_SCALE}")
    return units


def cash_units(price: int, size: int) -> int:
    """Notional of `size` shares at `price`, in cash units."""
    return price * size


def to_usd(cash: int) -> float:
    return cash / CASH_SCALE


def usd_to_cash(usd: float) -> int:
    """Round a USD amount to whole micro-USDC, expressed in cash units."""
    return round(usd * SIZE_SCALE) * PRICE_SCALE


def sign(side: Side) -> int:
    """+1 for BUY, -1 for SELL."""
    return 1 if side == "BUY" else -1


def opposite(side: Side) -> Side:
    return "SELL" if side == "BUY" else "BUY"


def crosses(side: Side, limit: int, level: int) -> bool:
    """True if an order on `side` with `limit` can trade at resting `level`."""
    return level <= limit if side == "BUY" else level >= limit
