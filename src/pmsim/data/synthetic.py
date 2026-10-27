"""Seeded synthetic market data. Every result built on it is labeled "synthetic".

Each binary market follows a random walk in log-odds. Books are quoted around
the probability, and the NO book mirrors the YES book. Configurable defects:
flash gaps (jumps), stale quotes (frozen books while the price moves), thin
depth, halts (empty books), and injected mispricings where YES ask + NO ask < 1.
Neg-risk groups share one probability vector that sums to 1.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.l2 import Level
from pmsim.book.replay import BookSnapshot, MarketEvent, TradePrint
from pmsim.book.units import PRICE_SCALE, size_units
from pmsim.data.kalshi import MockQuoteFeed, Quote, leg_token
from pmsim.engine.fees import FeeModel, MarketRules
from pmsim.engine.universe import CrossLink, LogicalLink, MarketData, Universe
from pmsim.risk.negrisk import GroupMarket, NegRiskGroup
from pmsim.risk.resolution import HOUR_MS, Resolution, binary_payouts, group_payouts
from pmsim.strategies.base import TokenMeta


class SyntheticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int = 42
    n_markets: int = Field(default=4, ge=0)
    n_groups: int = Field(default=1, ge=0)
    group_size: int = Field(default=3, ge=2)
    duration_hours: float = Field(default=6, gt=0)
    step_ms: int = Field(default=5_000, gt=0)
    tick: float = 0.01
    spread_ticks: int = Field(default=2, ge=1)
    levels: int = Field(default=3, ge=1)
    depth_shares: float = Field(default=200, gt=0)
    vol_per_hour: float = Field(default=0.3, ge=0)  # log-odds sd per sqrt(hour)
    p_flash_gap: float = Field(default=0.002, ge=0, le=1)
    flash_gap_logodds: float = Field(default=1.0, ge=0)
    p_stale: float = Field(default=0.003, ge=0, le=1)
    stale_steps: int = Field(default=12, ge=1)
    thin_fraction: float = Field(default=0.25, ge=0, le=1)
    thin_scale: float = Field(default=0.1, gt=0)
    p_halt: float = Field(default=0.001, ge=0, le=1)
    halt_steps: int = Field(default=24, ge=1)
    p_mispricing: float = Field(default=0.01, ge=0, le=1)
    mispricing_ticks: int = Field(default=3, ge=1)
    trades_per_step: float = Field(default=0.5, ge=0)
    fee_rate: float = Field(default=0.05, ge=0)  # docs "Other / General" taker rate
    kalshi_noise: float = Field(default=0.02, ge=0)
    p_kalshi_disagree: float = Field(default=0.02, ge=0, le=1)  # rule mismatch risk
    n_nested: int = Field(default=1, ge=0)  # subset/superset market pairs
    p_nested_violation: float = Field(default=0.01, ge=0, le=1)
    violation_ticks: int = Field(default=4, ge=1)


@dataclass
class _Book:
    """State of one synthetic order book pair (YES and NO)."""

    yes: str
    no: str
    depth: int
    stale_left: int = 0
    halt_left: int = 0


@dataclass
class _Nested:
    """A superset market and a subset market whose probability is a fraction of it."""

    sup: _Book
    sub: _Book
    x: float  # superset log-odds
    y: float  # log-odds of the subset share


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


class Generator:
    def __init__(self, cfg: SyntheticConfig) -> None:
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.tick = round(cfg.tick * PRICE_SCALE)
        self.steps = int(cfg.duration_hours * HOUR_MS // cfg.step_ms)
        self.end_ts = self.steps * cfg.step_ms
        self.events: list[MarketEvent] = []

    # ---- quoting -------------------------------------------------------------

    def _levels(self, best: int, step: int, depth: int) -> tuple[Level, ...]:
        out = []
        for i in range(self.cfg.levels):
            price = best + step * i * self.tick
            if self.tick <= price <= PRICE_SCALE - self.tick:
                out.append((price, max(1, int(depth * self.rng.uniform(0.5, 1.5)))))
        return tuple(out)

    def _quote(self, book: _Book, p: float, ts: int) -> None:
        """Emit YES and NO snapshots around probability `p`."""
        cfg, tick = self.cfg, self.tick
        mid = round(p * PRICE_SCALE / tick) * tick
        half = cfg.spread_ticks * tick // 2 or tick
        bid = min(max(tick, mid - half), PRICE_SCALE - 2 * tick)
        ask = max(bid + tick, min(PRICE_SCALE - tick, mid + half))
        no_bid, no_ask = PRICE_SCALE - ask, PRICE_SCALE - bid
        if self.rng.random() < cfg.p_mispricing:
            # both asks drop, so YES ask + NO ask < 1; bids move down to stay below
            cut = cfg.mispricing_ticks * tick
            ask, no_ask = max(tick * 2, ask - cut), max(tick * 2, no_ask - cut)
            bid, no_bid = min(bid, ask - tick), min(no_bid, no_ask - tick)
        for token, b, a in ((book.yes, bid, ask), (book.no, no_bid, no_ask)):
            bids = self._levels(b, -1, book.depth)
            asks = self._levels(a, 1, book.depth)
            self.events.append(BookSnapshot(token, ts, ts, bids, asks))
            self._trades(token, b, a, ts)

    def _trades(self, token: str, bid: int, ask: int, ts: int) -> None:
        n = self._poisson(self.cfg.trades_per_step)
        for k in range(n):
            price = bid if self.rng.random() < 0.5 else ask
            size = size_units(self.rng.uniform(5, self.cfg.depth_shares / 2))
            self.events.append(TradePrint(token, ts + 1 + k, ts + 1 + k, price, size))

    def _poisson(self, lam: float) -> int:
        n, p, limit = 0, 1.0, math.exp(-lam)
        while True:
            p *= self.rng.random()
            if p <= limit:
                return n
            n += 1

    def _step_book(self, book: _Book, p: float, ts: int) -> None:
        cfg = self.cfg
        if book.halt_left:
            book.halt_left -= 1
            return
        if self.rng.random() < cfg.p_halt:
            book.halt_left = cfg.halt_steps
            for token in (book.yes, book.no):
                self.events.append(BookSnapshot(token, ts, ts, (), ()))
            return
        if book.stale_left:
            book.stale_left -= 1
            return
        if self.rng.random() < cfg.p_stale:
            book.stale_left = cfg.stale_steps
        self._quote(book, p, ts)

    def _shock(self) -> float:
        cfg = self.cfg
        sd = cfg.vol_per_hour * math.sqrt(cfg.step_ms / HOUR_MS)
        x = self.rng.gauss(0, sd)
        if self.rng.random() < cfg.p_flash_gap:
            x += cfg.flash_gap_logodds * self.rng.choice((-1, 1))
        return x

    def _new_book(self, yes: str, no: str) -> _Book:
        thin = self.rng.random() < self.cfg.thin_fraction
        depth = size_units(self.cfg.depth_shares * (self.cfg.thin_scale if thin else 1))
        return _Book(yes, no, depth)

    # ---- build -----------------------------------------------------------------

    def build(self) -> MarketData:
        cfg = self.cfg
        rules = MarketRules(self.tick, size_units(5), FeeModel(cfg.fee_rate))
        meta: dict[str, TokenMeta] = {}
        all_rules: dict[str, MarketRules] = {}
        resolutions: list[Resolution] = []
        quotes: dict[str, list[Quote]] = {}
        links: list[CrossLink] = []
        links_logical: list[LogicalLink] = []

        def add_meta(yes: str, no: str, mid: str, group: str = "") -> None:
            for i, (tok, comp) in enumerate(((yes, no), (no, yes))):
                meta[tok] = TokenMeta(
                    tok, mid, i, comp, self.end_ts, ("synthetic",), "Synthetic market.", group
                )
                all_rules[tok] = rules

        binaries = []
        for m in range(cfg.n_markets):
            yes, no = f"syn{m}-yes", f"syn{m}-no"
            add_meta(yes, no, f"syn{m}")
            binaries.append((self._new_book(yes, no), self.rng.uniform(-2, 2)))
            links.append(CrossLink(f"syn{m}", yes, no, f"K-syn{m}", 0.1))
        groups = []
        group_state = []
        for g in range(cfg.n_groups):
            markets, books = [], []
            for i in range(cfg.group_size):
                yes, no = f"grp{g}-{i}-yes", f"grp{g}-{i}-no"
                add_meta(yes, no, f"grp{g}-{i}", f"grp{g}")
                markets.append(GroupMarket(f"grp{g}-{i}", f"outcome {i}", yes, no, False))
                books.append(self._new_book(yes, no))
            groups.append(NegRiskGroup(f"grp{g}", tuple(markets)))
            group_state.append((books, [self.rng.gauss(0, 1) for _ in range(cfg.group_size)]))

        nested: list[_Nested] = []
        for k in range(cfg.n_nested):
            sup, sub = f"nest{k}-sup", f"nest{k}-sub"
            add_meta(f"{sup}-yes", f"{sup}-no", sup)
            add_meta(f"{sub}-yes", f"{sub}-no", sub)
            links_logical.append(LogicalLink(f"nest{k}", f"{sub}-no", f"{sup}-yes"))
            nested.append(
                _Nested(
                    self._new_book(f"{sup}-yes", f"{sup}-no"),
                    self._new_book(f"{sub}-yes", f"{sub}-no"),
                    self.rng.uniform(-1, 1),
                    self.rng.uniform(-1, 1),
                )
            )

        for step in range(self.steps):
            ts = step * cfg.step_ms
            for i, (book, x) in enumerate(binaries):
                x += self._shock()
                binaries[i] = (book, x)
                p = _sigmoid(x)
                self._step_book(book, p, ts)
                quotes.setdefault(f"K-syn{i}", []).append(self._kalshi(p, ts))
            for books, xs in group_state:
                for j in range(len(xs)):
                    xs[j] += self._shock()
                probs = _softmax(xs)
                for book, p in zip(books, probs, strict=True):
                    self._step_book(book, p, ts)
            for n in nested:
                n.x += self._shock()
                n.y += self._shock()
                p_sup = _sigmoid(n.x)
                p_sub = p_sup * _sigmoid(n.y)
                if self.rng.random() < cfg.p_nested_violation:
                    p_sub = min(0.98, p_sup + cfg.violation_ticks * self.tick / PRICE_SCALE)
                self._step_book(n.sup, p_sup, ts)
                self._step_book(n.sub, p_sub, ts)

        res_ts = self.end_ts + 2 * HOUR_MS
        for m, (book, x) in enumerate(binaries):
            yes_wins = self.rng.random() < _sigmoid(x)
            payouts = binary_payouts(book.yes, book.no, "winner", yes_wins)
            k_yes = yes_wins != (self.rng.random() < cfg.p_kalshi_disagree)
            ticker = f"K-syn{m}"
            payouts.update(
                binary_payouts(leg_token(ticker, "yes"), leg_token(ticker, "no"), "winner", k_yes)
            )
            resolutions.append(Resolution(res_ts, "winner", payouts))
        for group, (_, xs) in zip(groups, group_state, strict=True):
            winner = self._pick(_softmax(xs))
            resolutions.append(Resolution(res_ts, "winner", group_payouts(group, winner)))

        for n in nested:
            sup_wins = self.rng.random() < _sigmoid(n.x)
            sub_wins = sup_wins and self.rng.random() < _sigmoid(n.y)
            payouts = binary_payouts(n.sup.yes, n.sup.no, "winner", sup_wins)
            payouts.update(binary_payouts(n.sub.yes, n.sub.no, "winner", sub_wins))
            resolutions.append(Resolution(res_ts, "winner", payouts))

        uni = Universe(
            meta,
            all_rules,
            "synthetic",
            groups,
            resolutions,
            MockQuoteFeed(quotes),
            links,
            links_logical,
        )
        self.events.sort(key=lambda e: (e.ts, e.recv_ts))
        return MarketData(uni, self.events)

    def _kalshi(self, p: float, ts: int) -> Quote:
        tick = self.tick
        q = min(0.98, max(0.02, p + self.rng.gauss(0, self.cfg.kalshi_noise)))
        mid = round(q * PRICE_SCALE / tick) * tick
        depth = size_units(self.cfg.depth_shares)
        return Quote(ts, mid - tick, depth, mid + tick, depth)

    def _pick(self, probs: list[float]) -> int:
        u, acc = self.rng.random(), 0.0
        for i, p in enumerate(probs):
            acc += p
            if u < acc:
                return i
        return len(probs) - 1


def _softmax(xs: list[float]) -> list[float]:
    top = max(xs)
    exps = [math.exp(x - top) for x in xs]
    total = sum(exps)
    return [e / total for e in exps]


def generate(cfg: SyntheticConfig | None = None) -> MarketData:
    return Generator(cfg or SyntheticConfig()).build()
