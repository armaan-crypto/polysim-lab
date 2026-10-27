"""Cost and edge helpers shared by strategies. All amounts in cash units."""

from __future__ import annotations

from collections.abc import Sequence

from pmsim.book.units import PRICE_SCALE
from pmsim.engine.fees import MarketRules

Leg = tuple[int, MarketRules]  # (price, rules) of one taker buy


def buy_cost(price: int, size: int, rules: MarketRules, gas: int) -> int:
    """Cash to take `size` shares at `price`, fee and gas included."""
    return price * size + rules.fees.fee(price, size, "taker") + gas


def set_edge(legs: Sequence[Leg], size: int, payout_shares: int, gas: int) -> int:
    """Edge of buying `size` of every leg when the set pays `payout_shares` dollars per unit.

    Complementary pair: payout 1, so edge = 1 - (P_YES + P_NO) - fees - gas.
    """
    cost = sum(buy_cost(p, size, r, gas) for p, r in legs)
    return payout_shares * PRICE_SCALE * size - cost


def snap_down(price: float, tick: int) -> int:
    return int(price // tick) * tick


def snap_up(price: float, tick: int) -> int:
    return -int(-price // tick) * tick


def per_share(usd: float, size: int) -> int:
    """`usd` dollars per share over `size` share units, in cash units."""
    return round(usd * PRICE_SCALE) * size
