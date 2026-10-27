"""No-code trading bots: one simple rule, run in backtests or live on the paper account.

A rule says when to buy (price falls below, or rises above, an entry price),
when to sell (take-profit and stop-loss on the bid), how much to spend per
order and how much exposure to allow. `decide` holds the logic and is shared
by the backtest strategy (`RuleBot`) and the live runner (`BotManager`), so
a bot behaves the same in both.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pmsim.book.replay import BookSnapshot, MarketEvent
from pmsim.book.units import PRICE_SCALE, SIZE_SCALE, price_units, size_units
from pmsim.data.models import PricePoint
from pmsim.engine.fees import FeeModel, MarketRules
from pmsim.engine.universe import MarketData, Universe
from pmsim.live.account import Instrument, PaperAccount, TradeError
from pmsim.live.data import LiveData
from pmsim.strategies.base import BookUpdate, Intent, OrderIntent, Strategy, TokenMeta

LOG_KEEP = 200


class BotRule(BaseModel):
    """When to buy and sell. Prices are dollars per share (0 to 1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: Literal["below", "above"] = Field(
        default="below",
        title="Buy when the price is",
        description="below: buy dips. above: buy breakouts.",
    )
    entry_price: float = Field(
        default=0.4, gt=0, lt=1, title="Entry price", description="Price that triggers a buy."
    )
    take_profit: float | None = Field(
        default=None, gt=0, lt=1, title="Take profit", description="Sell everything at this bid."
    )
    stop_loss: float | None = Field(
        default=None, gt=0, lt=1, title="Stop loss", description="Sell everything at this bid."
    )
    order_usd: float = Field(
        default=25, ge=1, title="Spend per buy ($)", description="Dollars spent on each buy."
    )
    max_position_usd: float = Field(
        default=100, ge=1, title="Max position ($)", description="Stop buying above this exposure."
    )
    cooldown_s: float = Field(
        default=60, ge=0, title="Wait between trades (s)", description="Pause after each trade."
    )

    @model_validator(mode="after")
    def _ordered(self) -> BotRule:
        if self.take_profit is not None and self.stop_loss is not None:
            if self.stop_loss >= self.take_profit:
                raise ValueError("stop loss must be below take profit")
        return self


@dataclass(frozen=True)
class Action:
    side: Literal["BUY", "SELL"]
    amount: float  # dollars for buys, shares for sells
    reason: str


def decide(rule: BotRule, bid: float | None, ask: float | None, shares: float) -> Action | None:
    """What the rule wants to do at these prices, holding `shares`."""
    if shares > 0 and bid is not None:
        if rule.take_profit is not None and bid >= rule.take_profit:
            return Action("SELL", shares, f"bid {bid:.3f} reached take profit {rule.take_profit:g}")
        if rule.stop_loss is not None and bid <= rule.stop_loss:
            return Action("SELL", shares, f"bid {bid:.3f} hit stop loss {rule.stop_loss:g}")
    if ask is None:
        return None
    triggered = ask <= rule.entry_price if rule.entry == "below" else ask >= rule.entry_price
    exposure = shares * ask
    if triggered and exposure + rule.order_usd <= rule.max_position_usd + 1e-9:
        word = "at or below" if rule.entry == "below" else "at or above"
        return Action("BUY", rule.order_usd, f"ask {ask:.3f} is {word} {rule.entry_price:g}")
    return None


# ---- backtests ------------------------------------------------------------------------


class RuleBotConfig(BotRule):
    tokens: list[str] = Field(default_factory=list, title="Tokens")  # empty: every YES token


class RuleBot(Strategy):
    """The bot rule as a backtest strategy."""

    name = "rule_bot"

    def __init__(self, cfg: RuleBotConfig | None = None) -> None:
        self.cfg = cfg or RuleBotConfig()
        self._last: dict[str, int] = {}

    def _targets(self, token: str) -> bool:
        if self.cfg.tokens:
            return token in self.cfg.tokens
        meta = self.view.meta.get(token)
        return meta is not None and meta.outcome_index == 0

    def on_book(self, update: BookUpdate) -> list[Intent]:
        token, view = update.token, self.view
        if not self._targets(token) or token not in view.rules:
            return []
        if view.now - self._last.get(token, -(10**15)) < self.cfg.cooldown_s * 1000:
            return []
        if view.open_orders(token):
            return []
        book = view.book(token)
        if book is None:
            return []
        bid = book.best_bid[0] / PRICE_SCALE if book.best_bid else None
        ask = book.best_ask[0] / PRICE_SCALE if book.best_ask else None
        held = view.position(token) / SIZE_SCALE
        action = decide(self.cfg, bid, ask, held)
        if action is None:
            return []
        self._last[token] = view.now
        if action.side == "SELL" and book.best_bid:
            size = min(view.position(token), book.best_bid[1])
            return [OrderIntent(token, "SELL", book.best_bid[0], size, "IOC", tag=action.reason)]
        if action.side == "BUY" and book.best_ask:
            px, avail = book.best_ask
            size = min(avail, size_units(action.amount / (px / PRICE_SCALE)))
            if size < view.rules[token].min_size:
                return []
            return [OrderIntent(token, "BUY", px, size, "IOC", tag=action.reason)]
        return []


def history_data(
    points: list[PricePoint],
    yes: str,
    no: str,
    tick: float,
    label: str,
    depth_shares: float = 5_000,
) -> MarketData:
    """Replay a market's real price history with a simulated book around each price.

    The spread is one tick each side of the historical price; depth is assumed.
    """
    t = price_units(tick)
    depth = size_units(depth_shares)
    events: list[MarketEvent] = []
    for p in sorted(points, key=lambda x: x.t):
        mid = round(price_units(p.p) / t) * t
        bid = min(max(t, mid - t), PRICE_SCALE - 2 * t)
        ask = bid + 2 * t if mid - t >= t else bid + t
        ask = min(ask, PRICE_SCALE - t)
        ts = p.t * 1000
        events.append(BookSnapshot(yes, ts, ts, ((bid, depth),), ((ask, depth),)))
        events.append(
            BookSnapshot(no, ts, ts, ((PRICE_SCALE - ask, depth),), ((PRICE_SCALE - bid, depth),))
        )
    rules = MarketRules(t, size_units(1), FeeModel(0.0))
    meta = {
        yes: TokenMeta(yes, label, 0, no, None),
        no: TokenMeta(no, label, 1, yes, None),
    }
    return MarketData(Universe(meta, {yes: rules, no: rules}, "historical"), events)


# ---- live bots -------------------------------------------------------------------------


class BotSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = Field(min_length=1, max_length=60)
    instrument: dict[str, Any]
    rule: BotRule
    interval_s: float = Field(default=10, ge=2, le=3600)
    created_ms: int = Field(default_factory=lambda: int(time.time() * 1000))
    running: bool = False


@dataclass
class BotStats:
    shares: float = 0.0
    cost: float = 0.0
    realized: float = 0.0
    trades: int = 0
    last_run_ms: int = 0
    last_trade_ms: int = 0
    last_price: float | None = None


class BotManager:
    """Owns bot specs, their stats and logs, and runs the live ones on the paper account."""

    def __init__(
        self,
        root: Path,
        account: PaperAccount,
        data: LiveData,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.root = root
        self.account = account
        self.data = data
        self.clock_ms = clock_ms
        self.lock = threading.RLock()
        self.bots: dict[str, BotSpec] = {}
        self.stats: dict[str, BotStats] = {}
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self._load()

    @property
    def _path(self) -> Path:
        return self.root / "bots.json"

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text())
        for item in raw["bots"]:
            spec = BotSpec.model_validate(item["spec"])
            self.bots[spec.id] = spec
            self.stats[spec.id] = BotStats(**item["stats"])
            self.logs[spec.id] = item["log"]

    def save(self) -> None:
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {
                "bots": [
                    {
                        "spec": self.bots[b].model_dump(),
                        "stats": self.stats[b].__dict__,
                        "log": self.logs[b][-LOG_KEEP:],
                    }
                    for b in self.bots
                ]
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(self._path)

    def create(self, raw: dict[str, Any]) -> BotSpec:
        spec = BotSpec.model_validate(raw)
        Instrument(**spec.instrument)  # validates the instrument shape
        with self.lock:
            self.bots[spec.id] = spec
            self.stats[spec.id] = BotStats()
            self.logs[spec.id] = []
            self._log(spec.id, "created", f"rule: {describe_rule(spec.rule)}")
            self.save()
        return spec

    def delete(self, bot_id: str) -> None:
        with self.lock:
            self._get(bot_id)
            del self.bots[bot_id], self.stats[bot_id], self.logs[bot_id]
            self.save()

    def set_running(self, bot_id: str, running: bool) -> BotSpec:
        with self.lock:
            spec = self._get(bot_id)
            spec.running = running
            self._log(bot_id, "started" if running else "stopped", "by you")
            self.save()
            return spec

    def _get(self, bot_id: str) -> BotSpec:
        if bot_id not in self.bots:
            raise KeyError(f"no bot {bot_id!r}")
        return self.bots[bot_id]

    def _log(self, bot_id: str, kind: str, detail: str) -> None:
        self.logs[bot_id].append({"ts": self.clock_ms(), "kind": kind, "detail": detail})
        del self.logs[bot_id][:-LOG_KEEP]

    def tick(self) -> None:
        """Run every live bot whose interval has passed."""
        now = self.clock_ms()
        for bot_id, spec in list(self.bots.items()):
            st = self.stats.get(bot_id)
            if not spec.running or st is None or now - st.last_run_ms < spec.interval_s * 1000:
                continue
            st.last_run_ms = now
            try:
                self._run_once(spec, st, now)
            except Exception as exc:  # network or book trouble: log and keep going
                with self.lock:
                    self._log(bot_id, "error", f"{type(exc).__name__}: {exc}")
        with self.lock:
            self.save()

    def _run_once(self, spec: BotSpec, st: BotStats, now: int) -> None:
        inst = Instrument(**spec.instrument)
        book = self.data.book(inst.token)
        bid = max((lv.price for lv in book.bids), default=None)
        ask = min((lv.price for lv in book.asks), default=None)
        st.last_price = ask if ask is not None else bid
        if now - st.last_trade_ms < spec.rule.cooldown_s * 1000:
            return
        held = min(st.shares, self.account.free_shares(inst.token) / SIZE_SCALE)
        action = decide(spec.rule, bid, ask, held)
        if action is None:
            return
        try:
            if action.side == "BUY":
                amount = min(action.amount, self.account.free_cash() / 10**10)
                row = self.account.market_buy(inst, book, amount, bot=spec.id)
                st.shares += row["shares"]
                st.cost += row["notional"] + row["fee"]
            else:
                row = self.account.market_sell(inst, book, action.amount, bot=spec.id)
                basis = st.cost * row["shares"] / st.shares if st.shares else 0.0
                st.realized += row["notional"] - row["fee"] - basis
                st.cost -= basis
                st.shares -= row["shares"]
        except TradeError as exc:
            with self.lock:
                self._log(spec.id, "skipped", f"{action.reason}; {exc}")
            return
        st.trades += 1
        st.last_trade_ms = now
        with self.lock:
            verb = "bought" if action.side == "BUY" else "sold"
            self._log(
                spec.id,
                action.side.lower(),
                f"{verb} {row['shares']:,.2f} @ {row['avg_price'] * 100:.1f}¢"
                f" because {action.reason}",
            )

    def view(self, bot_id: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            ids = [bot_id] if bot_id else list(self.bots)
            out = []
            for b in ids:
                spec, st = self._get(b), self.stats[b]
                value = st.shares * (st.last_price or 0)
                out.append(
                    {
                        **spec.model_dump(),
                        "summary": describe_rule(spec.rule),
                        "stats": {
                            **st.__dict__,
                            "value": value,
                            "pnl": st.realized + value - st.cost,
                        },
                        "log": list(reversed(self.logs[b][-60:])),
                    }
                )
            return out


def describe_rule(rule: BotRule) -> str:
    parts = [f"Buy ${rule.order_usd:g} when the price is {rule.entry} {rule.entry_price * 100:g}¢"]
    if rule.take_profit is not None:
        parts.append(f"sell at {rule.take_profit * 100:g}¢")
    if rule.stop_loss is not None:
        parts.append(f"stop out at {rule.stop_loss * 100:g}¢")
    parts.append(f"hold at most ${rule.max_position_usd:g}")
    return ", ".join(parts) + "."
