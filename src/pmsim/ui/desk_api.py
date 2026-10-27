"""Live desk API: real Polymarket data, the paper account, and bots.

Market data is read live from Polymarket's public APIs. Every trade goes to
the simulated `PaperAccount`; nothing here can send an order to Polymarket.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from pmsim.config import Config
from pmsim.data.models import Market
from pmsim.engine.backtest import Backtest, BacktestConfig
from pmsim.live.account import Instrument, PaperAccount, Side, TradeError
from pmsim.live.bots import BotManager, BotRule, RuleBot, RuleBotConfig, history_data
from pmsim.live.data import LiveData, LiveEvent
from pmsim.live.worker import Desk
from pmsim.ui.api import ApiError, result_payload, summary

INTERVALS = ("1h", "6h", "1d", "1w", "1m", "max")
BOOK_DEPTH = 15


@dataclass
class DeskContext:
    cfg: Config
    data: LiveData
    account: PaperAccount
    bots: BotManager
    desk: Desk


def make_context(cfg: Config, data: LiveData | None = None) -> DeskContext:
    live = data or LiveData(cfg.api)
    account = PaperAccount(cfg.data_dir / "desk")
    bots = BotManager(cfg.data_dir / "desk", account, live)
    return DeskContext(cfg, live, account, bots, Desk(account, live, bots))


# ---- market data ------------------------------------------------------------------


def _market_json(ev: LiveEvent, m: Market) -> dict[str, Any]:
    return {
        "id": m.id,
        "slug": m.slug,
        "label": m.group_item_title or m.question,
        "question": m.question,
        "outcomes": m.outcomes,
        "prices": m.outcome_prices,
        "tokens": m.token_ids,
        "volume": m.volume,
        "tick": m.tick_size,
        "min_size": m.min_order_size,
        "fee_rate": m.fee_schedule.rate if m.fee_type else None,
        "accepting": m.accepting_orders,
        "end": m.end_date.isoformat() if m.end_date else None,
    }


def _event_json(ev: LiveEvent, full: bool = False) -> dict[str, Any]:
    markets = sorted(ev.markets, key=lambda m: -(m.outcome_prices[0] if m.outcome_prices else 0))
    out = {
        "id": ev.id,
        "slug": ev.slug,
        "title": ev.title,
        "icon": ev.icon,
        "end": ev.end,
        "volume": ev.volume,
        "volume_24h": ev.volume_24h,
        "neg_risk": ev.neg_risk,
        "tags": list(ev.tags[:4]),
        "markets": [_market_json(ev, m) for m in markets],
    }
    if full:
        out["description"] = ev.description
    return out


def _live(fn: Any, *args: Any) -> Any:
    """Call a live data function, turning network failures into readable errors."""
    try:
        return fn(*args)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            f"could not reach Polymarket ({type(exc).__name__}). Check your connection."
        ) from exc


def live_events(
    ctx: DeskContext, query: str = "", tag: str = "", limit: int = 60
) -> dict[str, Any]:
    events = _live(ctx.data.events)
    q = query.lower().strip()
    if q:
        events = [
            e
            for e in events
            if q in e.title.lower() or any(q in (m.question or "").lower() for m in e.markets)
        ]
    if tag:
        events = [e for e in events if tag in e.tags]
    counts: dict[str, int] = {}
    for e in _live(ctx.data.events):
        for t in e.tags[:4]:
            counts[t] = counts.get(t, 0) + 1
    top_tags = sorted(counts, key=lambda t: -counts[t])[:14]
    return {
        "events": [_event_json(e) for e in events[: min(limit, 200)]],
        "total": len(events),
        "tags": top_tags,
        "as_of": int(time.time() * 1000),
    }


def live_event(ctx: DeskContext, slug: str) -> dict[str, Any]:
    ev = _live(ctx.data.event, slug)
    if ev is None:
        raise ApiError(f"no open market {slug!r}")
    return _event_json(ev, full=True)


def live_book(ctx: DeskContext, token: str) -> dict[str, Any]:
    if not token:
        raise ApiError("token is required")
    book = _live(ctx.data.book, token)
    bids = sorted(((lv.price, lv.size) for lv in book.bids), reverse=True)[:BOOK_DEPTH]
    asks = sorted((lv.price, lv.size) for lv in book.asks)[:BOOK_DEPTH]

    def levels(rows: list[tuple[float, float]]) -> list[dict[str, float]]:
        out, total = [], 0.0
        for price, size in rows:
            total += price * size
            out.append({"price": price, "shares": size, "total": total})
        return out

    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    return {
        "token": token,
        "bids": levels(bids),
        "asks": levels(asks),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": best_ask - best_bid if best_ask is not None and best_bid is not None else None,
        "last": book.last_trade_price,
        "tick": book.tick_size,
        "as_of": book.timestamp_ms,
    }


def live_history(ctx: DeskContext, token: str, interval: str) -> dict[str, Any]:
    if interval not in INTERVALS:
        raise ApiError(f"interval must be one of {', '.join(INTERVALS)}")
    points = _live(ctx.data.history, token, interval)
    return {"token": token, "interval": interval, "points": [[p.t * 1000, p.p] for p in points]}


# ---- trading --------------------------------------------------------------------------


def instrument(ctx: DeskContext, event_slug: str, token: str) -> Instrument:
    ev = _live(ctx.data.event, event_slug)
    if ev is None:
        raise ApiError(f"no open market {event_slug!r}")
    for m in ev.markets:
        if token in m.token_ids:
            if not m.accepting_orders:
                raise ApiError("this market is not accepting orders right now")
            idx = m.token_ids.index(token)
            label = m.group_item_title or m.question
            name = ev.title if label == ev.title else f"{ev.title} / {label}"
            rate = m.fee_schedule.rate if m.fee_type else ctx.cfg.fees.default_taker_rate
            return Instrument(
                token=token,
                label=name,
                market_slug=m.slug,
                event_slug=ev.slug,
                outcome=m.outcomes[idx],
                outcome_index=idx,
                fee_rate=rate,
                tick=m.tick_size,
                min_size=m.min_order_size,
            )
    raise ApiError("that outcome is not part of this market")


def _num(body: Mapping[str, Any], key: str) -> float:
    try:
        return float(body[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(f"{key} must be a number") from exc


def _side(body: Mapping[str, Any]) -> Side:
    side = str(body.get("side", "")).upper()
    if side == "BUY":
        return "BUY"
    if side == "SELL":
        return "SELL"
    raise ApiError("side must be BUY or SELL")


def quote(ctx: DeskContext, body: Mapping[str, Any]) -> dict[str, Any]:
    inst = instrument(ctx, str(body.get("event_slug", "")), str(body.get("token", "")))
    book = _live(ctx.data.book, inst.token)
    return ctx.account.quote(inst, book, _side(body), _num(body, "amount"))


def trade(ctx: DeskContext, body: Mapping[str, Any]) -> dict[str, Any]:
    """Execute on the paper account. `type` is market (amount: $ to buy, shares to sell)
    or limit (price and shares)."""
    inst = instrument(ctx, str(body.get("event_slug", "")), str(body.get("token", "")))
    side = _side(body)
    book = _live(ctx.data.book, inst.token)
    try:
        if body.get("type", "market") == "limit":
            result = ctx.account.place_limit(
                inst,
                book,
                side,
                _num(body, "price"),
                _num(body, "shares"),
            )
        elif side == "BUY":
            result = {"filled": ctx.account.market_buy(inst, book, _num(body, "amount"))}
        else:
            result = {"filled": ctx.account.market_sell(inst, book, _num(body, "amount"))}
    except TradeError as exc:
        raise ApiError(str(exc)) from exc
    return {**result, "portfolio": portfolio(ctx)}


def remove_limit(ctx: DeskContext, body: Mapping[str, Any]) -> dict[str, Any]:
    try:
        ctx.account.cancel(int(_num(body, "id")))
    except TradeError as exc:
        raise ApiError(str(exc)) from exc
    return portfolio(ctx)


def reset(ctx: DeskContext, body: Mapping[str, Any]) -> dict[str, Any]:
    cash = _num(body, "cash") if "cash" in body else 10_000.0
    if not 100 <= cash <= 10_000_000:
        raise ApiError("starting cash must be between $100 and $10,000,000")
    for bot in ctx.bots.view():
        if bot["running"]:
            ctx.bots.set_running(bot["id"], False)
    ctx.account.reset(cash)
    return portfolio(ctx)


def portfolio(ctx: DeskContext) -> dict[str, Any]:
    marks: dict[str, float] = {}
    for token in ctx.account.open_tokens():
        try:
            b = live_book(ctx, token)
        except ApiError:
            continue
        mark = b["mid"] if b["mid"] is not None else b["last"]
        if mark is not None:
            marks[token] = mark
    snap = ctx.account.snapshot(marks)
    snap["trades"] = ctx.account.trades(100)
    snap["errors"] = ctx.desk.errors[-5:]
    return snap


# ---- bots -------------------------------------------------------------------------------


def _rule(raw: object) -> BotRule:
    try:
        return BotRule.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"]) or "rule"
        raise ApiError(f"{field}: {first['msg']}") from exc


def bots(ctx: DeskContext) -> dict[str, Any]:
    return {"bots": ctx.bots.view(), "rule_fields": BotRule.model_json_schema()["properties"]}


def create_bot(ctx: DeskContext, body: Mapping[str, Any]) -> dict[str, Any]:
    inst = instrument(ctx, str(body.get("event_slug", "")), str(body.get("token", "")))
    rule = _rule(body.get("rule", {}))
    name = str(body.get("name") or f"{inst.outcome} on {inst.label}")[:60]
    try:
        spec = ctx.bots.create(
            {
                "name": name,
                "instrument": inst.__dict__,
                "rule": rule.model_dump(),
                "interval_s": body.get("interval_s", 10),
                "running": bool(body.get("start", False)),
            }
        )
    except ValidationError as exc:
        raise ApiError(str(exc.errors()[0]["msg"])) from exc
    return ctx.bots.view(spec.id)[0]


def bot_action(ctx: DeskContext, bot_id: str, action: str) -> dict[str, Any]:
    try:
        if action == "start":
            ctx.bots.set_running(bot_id, True)
        elif action == "stop":
            ctx.bots.set_running(bot_id, False)
        elif action == "delete":
            ctx.bots.delete(bot_id)
            return bots(ctx)
        else:
            raise ApiError(f"unknown bot action {action!r}")
    except KeyError as exc:
        raise ApiError(str(exc)) from exc
    return ctx.bots.view(bot_id)[0]


def backtest_bot(ctx: DeskContext, body: Mapping[str, Any]) -> dict[str, Any]:
    """Run a bot rule over this market's real price history (books are modeled)."""
    inst = instrument(ctx, str(body.get("event_slug", "")), str(body.get("token", "")))
    rule = _rule(body.get("rule", {}))
    interval = str(body.get("interval", "1w"))
    if interval not in ("1d", "1w", "1m", "max"):
        raise ApiError("history window must be 1d, 1w, 1m or max")
    points = _live(ctx.data.history, inst.token, interval)
    if len(points) < 3:
        raise ApiError("not enough price history for this market yet")
    ev = _live(ctx.data.event, inst.event_slug)
    market = next(m for m in ev.markets if inst.token in m.token_ids)
    other = next(t for t in market.token_ids if t != inst.token)
    data = history_data(points, inst.token, other, inst.tick, inst.label)
    cfg = RuleBotConfig(**rule.model_dump(), tokens=[inst.token])
    started = time.perf_counter()
    res = Backtest(
        RuleBot(cfg), data, BacktestConfig(cash_usd=max(1000.0, rule.max_position_usd * 2))
    ).run()
    labels = {inst.token: f"{inst.label} / {inst.outcome}", other: f"{inst.label} / other side"}
    payload = result_payload(res, labels)
    payload["stress"] = None
    payload["data"] = {"kind": "historical", "window": interval, "points": len(points)}
    payload["elapsed_s"] = round(time.perf_counter() - started, 2)
    payload["summary"] = summary(payload)
    payload["prices"] = [[p.t * 1000, p.p] for p in points]
    payload["trades_on_chart"] = [
        [f["ts"], f["price"], f["side"]]
        for f in payload["fills"]
        if f["token"] == labels[inst.token]
    ]
    return payload
