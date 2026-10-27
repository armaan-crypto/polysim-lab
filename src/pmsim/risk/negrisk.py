"""Neg-risk (multi-outcome) events: groups of mutually exclusive binary markets.

Exactly one market in a group resolves YES. One NO share in any market
converts into one YES share in every other market of the group (docs,
concepts/negative-risk, checked 2026-10-27).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class GroupMarket:
    market_id: str
    title: str
    yes: str
    no: str
    is_other: bool  # the explicit "Other" outcome; docs advise against trading it


@dataclass(frozen=True)
class NegRiskGroup:
    group_id: str
    markets: tuple[GroupMarket, ...]

    def index_of(self, token: str) -> int:
        for i, m in enumerate(self.markets):
            if token in (m.yes, m.no):
                return i
        raise KeyError(token)

    def other_yes(self, i: int) -> list[str]:
        """YES tokens received when converting the NO of market `i`."""
        return [m.yes for j, m in enumerate(self.markets) if j != i]

    def yes_sum(self, yes_prices: Mapping[str, int]) -> int:
        """Sum of YES prices; 1.0 (PRICE_SCALE) for a fairly priced complete set."""
        return sum(yes_prices[m.yes] for m in self.markets)

    def implied_probabilities(self, yes_prices: Mapping[str, int]) -> list[float]:
        total = self.yes_sum(yes_prices)
        return [yes_prices[m.yes] / total if total else 0.0 for m in self.markets]

    def conversion_value(self, i: int, yes_bids: Mapping[str, int]) -> int:
        """Price units one converted NO of market `i` fetches by selling the YES legs."""
        return sum(yes_bids[t] for t in self.other_yes(i))


def groups_from_catalog(catalog: pl.DataFrame) -> list[NegRiskGroup]:
    """Build groups from catalog rows sharing a `neg_risk_market_id`."""
    rows = catalog.filter(pl.col("neg_risk") & (pl.col("neg_risk_market_id") != ""))
    groups = []
    for (group_id,), grp in rows.group_by(["neg_risk_market_id"], maintain_order=True):
        markets = []
        for (mid,), mk in grp.group_by(["market_id"], maintain_order=True):
            tokens = dict(mk.select("outcome_index", "token_id").iter_rows())
            first = mk.row(0, named=True)
            markets.append(
                GroupMarket(
                    str(mid),
                    first["group_item_title"],
                    tokens[0],
                    tokens[1],
                    first["neg_risk_other"],
                )
            )
        groups.append(NegRiskGroup(str(group_id), tuple(markets)))
    return groups
