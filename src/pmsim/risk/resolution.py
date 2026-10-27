"""Resolution: payouts at market end, settlement, and resolution-risk simulation.

Winning tokens redeem for $1 and losers for $0. Binary markets may resolve
50/50 ($0.50 each). Neg-risk groups never split: exactly one market wins.
A voided market refunds cost basis; Polymarket documents no void, so this
exists for other venues and stress tests.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pmsim.book.units import PRICE_SCALE
from pmsim.config import ResolutionConfig, ResolutionRisk
from pmsim.engine.portfolio import Portfolio
from pmsim.risk.negrisk import NegRiskGroup

HOUR_MS = 3600 * 1000
Kind = Literal["winner", "fifty_fifty", "void"]


@dataclass(frozen=True)
class Resolution:
    ts: int
    kind: Kind
    payouts: dict[str, int] = field(default_factory=dict)  # token -> price units per share
    voided: tuple[str, ...] = ()
    disputed: bool = False
    reversed: bool = False


@dataclass(frozen=True)
class RiskDraw:
    """One simulated resolution path: which outcome is paid, when, and how."""

    kind: Kind
    winner: int
    delay_ms: int
    disputed: bool
    reversed: bool


def binary_payouts(yes: str, no: str, kind: Kind, yes_wins: bool) -> dict[str, int]:
    if kind == "fifty_fifty":
        return {yes: PRICE_SCALE // 2, no: PRICE_SCALE // 2}
    if kind == "void":
        return {}
    return {yes: PRICE_SCALE if yes_wins else 0, no: 0 if yes_wins else PRICE_SCALE}


def group_payouts(group: NegRiskGroup, winner: int) -> dict[str, int]:
    payouts: dict[str, int] = {}
    for i, m in enumerate(group.markets):
        payouts.update(binary_payouts(m.yes, m.no, "winner", yes_wins=i == winner))
    return payouts


def settle(portfolio: Portfolio, res: Resolution, redeem_cost: int = 0) -> None:
    """Redeem every held token named by `res`. Redeem cost is charged per redemption."""
    for token, payout in res.payouts.items():
        if portfolio.size(token):
            portfolio.redeem(token, payout, res.ts, redeem_cost)
    for token in res.voided:
        portfolio.refund(token, res.ts)


def market_class(tags: Sequence[str], cfg: ResolutionConfig) -> str:
    for name, class_tags in cfg.classes.items():
        if any(t in tags for t in class_tags):
            return name
    return "default"


def risk_profile(tags: Sequence[str], cfg: ResolutionConfig) -> ResolutionRisk:
    """The class profile; fields a class leaves unset fall back to the defaults."""
    base = cfg.profiles.get("default", ResolutionRisk())
    own = cfg.profiles.get(market_class(tags, cfg))
    if own is None:
        return base
    return base.model_copy(update=own.model_dump(exclude_unset=True))


def draw_resolution(
    rng: random.Random, true_winner: int, n_outcomes: int, risk: ResolutionRisk, can_split: bool
) -> RiskDraw:
    """Sample delay, dispute, reversal, 50/50 and void for one market or group."""
    delay = risk.challenge_hours * HOUR_MS
    if rng.random() < risk.p_late_proposal and risk.late_proposal_mean_hours > 0:
        delay += rng.expovariate(1.0 / risk.late_proposal_mean_hours) * HOUR_MS
    u = rng.random()
    kind: Kind = "winner"
    if u < risk.p_void:
        kind = "void"
    elif can_split and u < risk.p_void + risk.p_fifty_fifty:
        kind = "fifty_fifty"
    disputed = rng.random() < risk.p_dispute
    reversed_ = False
    winner = true_winner
    if disputed:
        delay += risk.dispute_extra_hours * HOUR_MS
        if kind == "winner" and n_outcomes > 1 and rng.random() < risk.p_reversal:
            reversed_ = True
            winner = rng.choice([i for i in range(n_outcomes) if i != true_winner])
    return RiskDraw(kind, winner, int(delay), disputed, reversed_)


def simulate_binary(
    rng: random.Random, yes: str, no: str, yes_wins: bool, end_ts: int, risk: ResolutionRisk
) -> Resolution:
    draw = draw_resolution(rng, 0 if yes_wins else 1, 2, risk, can_split=True)
    payouts = binary_payouts(yes, no, draw.kind, yes_wins=draw.winner == 0)
    voided = (yes, no) if draw.kind == "void" else ()
    return Resolution(
        end_ts + draw.delay_ms, draw.kind, payouts, voided, draw.disputed, draw.reversed
    )


def simulate_group(
    rng: random.Random, group: NegRiskGroup, winner: int, end_ts: int, risk: ResolutionRisk
) -> Resolution:
    draw = draw_resolution(rng, winner, len(group.markets), risk, can_split=False)
    ts = end_ts + draw.delay_ms
    if draw.kind == "void":
        tokens = tuple(t for m in group.markets for t in (m.yes, m.no))
        return Resolution(ts, "void", {}, tokens, draw.disputed, draw.reversed)
    payouts = group_payouts(group, draw.winner)
    return Resolution(ts, "winner", payouts, (), draw.disputed, draw.reversed)
