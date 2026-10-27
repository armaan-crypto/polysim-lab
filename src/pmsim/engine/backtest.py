"""Event-driven backtest: recorded or synthetic events -> fills -> portfolio.

One loop drives timers, market events and resolutions in time order. The
strategy sees state through a `MarketView` and acts through intents; the
fill engine and a mock external venue turn intents into fills. Runs are
deterministic: the only randomness is the seeded resolution-risk draw.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace

from pydantic import BaseModel, ConfigDict, Field

from pmsim.book.replay import BookDelta, BookSnapshot, MarketEvent, TickChange, TradePrint
from pmsim.book.units import CASH_SCALE, PRICE_SCALE, Side, usd_to_cash
from pmsim.config import ResolutionRisk
from pmsim.data.kalshi import leg_token
from pmsim.engine.fees import FeeModel
from pmsim.engine.fills import Fill, FillEngine, OrderRequest
from pmsim.engine.portfolio import ClosedPosition, Portfolio
from pmsim.engine.universe import DataLabel, MarketData
from pmsim.risk.resolution import Resolution, draw_resolution, group_payouts, settle
from pmsim.strategies.base import (
    BookUpdate,
    CancelIntent,
    Clock,
    ExternalIntent,
    GuardedSeries,
    Intent,
    MarketView,
    OrderIntent,
    Strategy,
    known_at,
)
from pmsim.strategies.costs import buy_cost
from pmsim.strategies.legs import LegStats, LegStrategy


class BacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cash_usd: float = Field(default=10_000, gt=0)
    latency_ms: int = Field(default=150, ge=0)
    timer_ms: int = Field(default=1_000, gt=0)
    equity_every_ms: int = Field(default=60_000, gt=0)
    gas_per_trade_usd: float = Field(default=0.0, ge=0)
    redeem_cost_usd: float = Field(default=0.0, ge=0)
    kalshi_fee_rate: float = Field(default=0.07, ge=0)  # assumed, see PROGRESS
    seed: int = 42
    resolution_risk: ResolutionRisk | None = None  # None keeps resolutions as given
    tail_ms: int = Field(default=10_000, ge=0)  # timers run this long after the last event


@dataclass(frozen=True)
class Decision:
    ts: int
    kind: str
    detail: str
    reason: str


@dataclass
class BacktestResult:
    strategy: str
    label: DataLabel
    start_cash: int
    final_equity: int
    realized: int
    fills: list[Fill]
    expected_edge: int
    intents: int
    rejects: Counter[str]
    equity: list[tuple[int, int]]
    closed: list[ClosedPosition]
    legs: LegStats | None
    unwind_cost: int
    idle_cash_fraction: float
    resolutions: int
    resolution_losses: int = 0
    orders: int = 0
    orders_filled: int = 0
    edge_orders: int = 0  # orders that carried an expected edge (first legs, yield buys)
    decisions: list[Decision] = field(default_factory=list)

    @property
    def net_pnl(self) -> int:
        return self.final_equity - self.start_cash

    def net_pnl_usd(self) -> float:
        return self.net_pnl / CASH_SCALE


def _same_time(events: list[MarketEvent]) -> Iterator[list[MarketEvent]]:
    batch: list[MarketEvent] = []
    for ev in events:
        if batch and ev.ts != batch[0].ts:
            yield batch
            batch = []
        batch.append(ev)
    if batch:
        yield batch


class _Positions(Mapping[str, int]):
    """Live read-only view of portfolio sizes for the strategy."""

    def __init__(self, pf: Portfolio) -> None:
        self._pf = pf

    def __getitem__(self, token: str) -> int:
        if token not in self._pf.positions:
            raise KeyError(token)
        return self._pf.positions[token].size

    def __iter__(self) -> Iterator[str]:
        return iter(self._pf.positions)

    def __len__(self) -> int:
        return len(self._pf.positions)


class _Orders:
    def __init__(self, engine: FillEngine) -> None:
        self._engine = engine

    def open_order_ids(self, token: str) -> list[tuple[int, Side, int, int]]:
        return [
            (o.id, o.req.side, o.req.price, o.remaining) for o in self._engine.open_orders(token)
        ]


class Backtest:
    def __init__(
        self,
        strategy: Strategy,
        data: MarketData,
        cfg: BacktestConfig | None = None,
        start_ts: int | None = None,
        portfolio: Portfolio | None = None,
    ):
        """`start_ts` and `portfolio` let a live paper session start now or resume saved state."""
        self.cfg = cfg or BacktestConfig()
        self.strategy = strategy
        self.data = data
        uni = data.universe
        first = data.events[0].ts if data.events else 0
        self.clock = Clock(first if start_ts is None else start_ts)
        self.gas = usd_to_cash(self.cfg.gas_per_trade_usd)
        self.engine = FillEngine(uni.rules, self.cfg.latency_ms, self.gas)
        self.portfolio = portfolio or Portfolio(usd_to_cash(self.cfg.cash_usd), now=self.clock.now)
        self.kalshi_fees = FeeModel(self.cfg.kalshi_fee_rate)
        self.book_times: dict[str, int] = {}
        self.order_intent: dict[int, OrderIntent] = {}
        self.fills: list[Fill] = []
        self.rejects: Counter[str] = Counter()
        self.decisions: list[Decision] = []
        self.equity: list[tuple[int, int]] = []
        self.intents = 0
        self.expected_edge = 0
        self.resolution_losses = 0
        self.edge_orders = 0
        self.resolutions = sorted(self._perturb(uni.resolutions), key=lambda r: r.ts)
        trades: dict[str, list[TradePrint]] = {}
        for ev in data.events:
            if isinstance(ev, TradePrint):
                trades.setdefault(ev.token, []).append(ev)
        self.view = MarketView(
            self.clock,
            self.engine.books,
            self.book_times,
            uni.meta,
            self.engine.rules,
            _Positions(self.portfolio),
            lambda: self.portfolio.cash,
            uni.groups,
            self.gas,
            _Orders(self.engine),
            {
                t: GuardedSeries(self.clock, tr, lambda x: known_at(x.ts, x.recv_ts))
                for t, tr in trades.items()
            },
            self.engine.visible,
        )
        strategy.bind(self.view)

    # ---- main loop -----------------------------------------------------------

    def run(self) -> BacktestResult:
        events = self.data.events
        timer = self.clock.now
        next_equity = self.clock.now
        pending = list(self.resolutions)
        end = (events[-1].ts if events else 0) + self.cfg.tail_ms
        for batch in _same_time(events):
            ts = batch[0].ts
            while timer <= ts:
                next_equity = self.process_timer(timer, next_equity)
                timer += self.cfg.timer_ms
            while pending and pending[0].ts <= ts:
                self._resolve(pending.pop(0))
            self.process_batch(batch)
        while timer <= end:
            next_equity = self.process_timer(timer, next_equity)
            timer += self.cfg.timer_ms
        for res in pending:
            self._resolve(res)
        self._sample_equity(self.clock.now)
        return self._result()

    def process_batch(self, batch: list[MarketEvent]) -> None:
        """Apply every event stamped at one time, then let the strategy react once per token.

        One feed message often updates the YES and NO books together; reacting
        between them would show the strategy a half-updated pair.
        """
        touched: dict[str, None] = {}
        trades: list[TradePrint] = []
        for ev in batch:
            self.clock.set(max(self.clock.now, known_at(ev.ts, ev.recv_ts)))
            self._on_fills(self.engine.on_event(ev))
            if isinstance(ev, BookSnapshot | BookDelta):
                self.book_times[ev.token] = self.clock.now
                touched[ev.token] = None
            elif isinstance(ev, TradePrint):
                trades.append(ev)
            elif isinstance(ev, TickChange):
                self._log("tick", f"{ev.token} tick -> {ev.tick}", "venue changed tick size")
        for token in touched:
            self._submit(self.strategy.on_book(BookUpdate(token, self.clock.now)))
        for trade in trades:
            self._submit(self.strategy.on_trade(trade))

    def process_timer(self, t: int, next_equity: int) -> int:
        self.clock.set(max(self.clock.now, t))
        self._on_fills(self.engine.advance(t))
        self.portfolio.advance(max(self.portfolio.now, t))
        self._submit(self.strategy.on_timer(t))
        if t >= next_equity:
            self._sample_equity(t)
            next_equity = t + self.cfg.equity_every_ms
        return next_equity

    def _resolve(self, res: Resolution) -> None:
        self.clock.set(max(self.clock.now, res.ts))
        self._on_fills(self.engine.advance(res.ts))
        for token in (*res.payouts, *res.voided):
            self.engine.close_market(token, "market resolved")
        for token, payout in res.payouts.items():
            pos = self.portfolio.positions.get(token)
            if pos is not None and pos.size and pos.size * payout < pos.cost:
                self.resolution_losses += 1
        settle(self.portfolio, res, usd_to_cash(self.cfg.redeem_cost_usd))
        self._log(
            "resolution",
            f"{res.kind} disputed={res.disputed} reversed={res.reversed}",
            "market resolved; positions redeemed",
        )
        self._submit(self.strategy.on_resolution(res))

    # ---- intents and fills ----------------------------------------------------

    def _submit(self, intents: list[Intent]) -> None:
        for intent in intents:
            self.intents += 1
            if isinstance(intent, CancelIntent):
                if self.engine.cancel(intent.order_id, self.clock.now):
                    self._log("cancel", f"#{intent.order_id}", f"{self.strategy.name} requested")
                else:
                    self._reject("unknown order", intent)
            elif isinstance(intent, ExternalIntent):
                self._external(intent)
            elif reason := self._unaffordable(intent):
                self._reject(reason, intent)
            else:
                order = self.engine.submit(
                    OrderRequest(intent.token, intent.side, intent.price, intent.size, intent.tif),
                    self.clock.now,
                )
                self.order_intent[order.id] = intent
                self.expected_edge += intent.edge
                self.edge_orders += intent.edge != 0
                edge = intent.edge / CASH_SCALE
                why = f"{self.strategy.name}:{intent.tag or 'order'} edge ${edge:.4f}"
                self._log("order", f"#{order.id} {intent}", why)

    def _unaffordable(self, intent: OrderIntent) -> str:
        rules = self.engine.rules.get(intent.token)
        if rules is None:
            return "unknown token"
        open_orders = self.engine.open_orders()
        if intent.side == "BUY":
            reserved = sum(
                buy_cost(o.req.price, o.remaining, self.engine.rules[o.req.token], self.gas)
                for o in open_orders
                if o.req.side == "BUY"
            )
            need = buy_cost(intent.price, intent.size, rules, self.gas)
            return "insufficient cash" if need > self.portfolio.cash - reserved else ""
        selling = sum(
            o.remaining for o in open_orders if o.req.side == "SELL" and o.req.token == intent.token
        )
        held = self.portfolio.size(intent.token)
        return "insufficient shares" if intent.size > held - selling else ""

    def _apply(self, fill: Fill) -> Fill:
        """Book a fill. Fills from different queues can arrive a few ms out of order;
        those are booked at the portfolio's current time."""
        if fill.ts < self.portfolio.now:
            fill = replace(fill, ts=self.portfolio.now)
        self.portfolio.apply_fill(fill)
        self.fills.append(fill)
        return fill

    def _on_fills(self, fills: list[Fill]) -> None:
        for fill in fills:
            self._apply(fill)
            intent = self.order_intent[fill.order_id]
            self._log(
                "fill",
                f"#{fill.order_id} {fill.side} {fill.size}@{fill.price}",
                f"{fill.liquidity} fill under the fill model",
            )
            self._submit(self.strategy.on_fill(fill, intent))

    def _external(self, intent: ExternalIntent) -> None:
        """Mock venue: fill at the quote seen when the order arrives, if the limit allows."""
        feed = self.data.universe.feed
        arrive = self.clock.now + self.cfg.latency_ms
        quote = feed.quote(intent.ticker, arrive) if feed else None
        if quote is None:
            self._reject("no external quote", intent)
            return
        ask, avail = (
            (quote.yes_ask, quote.yes_ask_size)
            if intent.outcome == "yes"
            else (quote.no_ask, quote.yes_bid_size)
        )
        qty = min(intent.size, avail)
        fee = self.kalshi_fees.fee(ask, qty, "taker")
        if ask > intent.price or qty <= 0 or not 0 < ask < PRICE_SCALE:
            self._reject("external limit not met", intent)
            return
        if ask * qty + fee > self.portfolio.cash:
            self._reject("insufficient cash", intent)
            return
        token = leg_token(intent.ticker, intent.outcome)
        self._apply(Fill(-1, token, "BUY", ask, qty, arrive, "taker", fee, 0))
        self._log("external_fill", f"{token} {qty}@{ask}", "mock venue filled at its quote")
        self._submit(self.strategy.on_external_fill(intent, qty, ask))

    # ---- bookkeeping -------------------------------------------------------------

    def _reject(self, reason: str, intent: Intent) -> None:
        self.rejects[reason] += 1
        self._log("reject", f"{intent}", reason)

    def _log(self, kind: str, detail: str, reason: str) -> None:
        self.decisions.append(Decision(self.clock.now, kind, detail, reason))

    def marks(self) -> dict[str, int]:
        """Mark each held token at mid, else last trade, else its average cost."""
        out: dict[str, int] = {}
        for token, pos in self.portfolio.positions.items():
            if not pos.size:
                continue
            out[token] = self._mark(token, pos.cost // pos.size)
        return out

    def _mark(self, token: str, fallback: int) -> int:
        book = self.engine.books.get(token)
        if book and book.best("BUY") is not None and book.best("SELL") is not None:
            return (book.best("BUY") + book.best("SELL")) // 2  # type: ignore[operator]
        feed = self.data.universe.feed
        if token.startswith("kalshi:") and feed is not None:
            _, ticker, outcome = token.split(":")
            q = feed.quote(ticker, self.clock.now)
            if q is not None:
                mid = (q.yes_bid + q.yes_ask) // 2
                return mid if outcome == "yes" else PRICE_SCALE - mid
        series = self.view.trades.get(token)
        last = series.latest() if series else None
        return last.price if last else fallback

    def _sample_equity(self, t: int) -> None:
        self.equity.append((t, self.portfolio.equity(self.marks())))

    def _perturb(self, resolutions: list[Resolution]) -> list[Resolution]:
        risk = self.cfg.resolution_risk
        if risk is None:
            return list(resolutions)
        rng = random.Random(self.cfg.seed)
        return [self._perturb_one(rng, r, risk) for r in resolutions]

    def _perturb_one(self, rng: random.Random, res: Resolution, risk: ResolutionRisk) -> Resolution:
        uni = self.data.universe
        group = next((g for g in uni.groups if g.markets[0].yes in res.payouts), None)
        if group is not None:
            winner = next(i for i, m in enumerate(group.markets) if res.payouts[m.yes])
            draw = draw_resolution(rng, winner, len(group.markets), risk, can_split=False)
            payouts = group_payouts(group, draw.winner) if draw.kind == "winner" else {}
            gone = () if draw.kind == "winner" else tuple(res.payouts)
            return replace(
                res,
                ts=res.ts + draw.delay_ms,
                kind=draw.kind,
                payouts=payouts,
                voided=gone,
                disputed=draw.disputed,
                reversed=draw.reversed,
            )
        poly = [t for t in res.payouts if t in uni.meta]
        if len(poly) != 2:
            return replace(res, ts=res.ts + draw_resolution(rng, 0, 1, risk, False).delay_ms)
        yes = next(t for t in poly if uni.meta[t].outcome_index == 0)
        no = uni.meta[yes].complement
        draw = draw_resolution(rng, 0 if res.payouts[yes] else 1, 2, risk, can_split=True)
        payouts = dict(res.payouts)
        if draw.kind == "fifty_fifty":
            payouts[yes] = payouts[no] = PRICE_SCALE // 2
        elif draw.kind == "winner":
            payouts[yes] = PRICE_SCALE if draw.winner == 0 else 0
            payouts[no] = PRICE_SCALE - payouts[yes]
        voided: tuple[str, ...] = (yes, no) if draw.kind == "void" else ()
        for t in voided:
            payouts.pop(t)
        return replace(
            res,
            ts=res.ts + draw.delay_ms,
            kind=draw.kind,
            payouts=payouts,
            voided=voided,
            disputed=draw.disputed,
            reversed=draw.reversed,
        )

    def _result(self) -> BacktestResult:
        legs = self.strategy.legs if isinstance(self.strategy, LegStrategy) else None
        return BacktestResult(
            strategy=self.strategy.name,
            label=self.data.universe.label,
            start_cash=self.portfolio.initial_cash,
            final_equity=self.portfolio.equity(self.marks()),
            realized=self.portfolio.realized(),
            fills=self.fills,
            expected_edge=self.expected_edge,
            intents=self.intents,
            rejects=self.rejects,
            equity=self.equity,
            closed=self.portfolio.closed,
            legs=legs.stats if legs else None,
            unwind_cost=legs.unwind_cost() if legs else 0,
            idle_cash_fraction=self.portfolio.idle_cash_fraction(),
            resolutions=len(self.resolutions),
            resolution_losses=self.resolution_losses,
            orders=len(self.order_intent),
            edge_orders=self.edge_orders,
            orders_filled=len({f.order_id for f in self.fills if f.order_id > 0}),
            decisions=self.decisions,
        )
