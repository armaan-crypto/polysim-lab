"""Live order books from Polymarket's public market WebSocket.

One background connection subscribes to every token the dashboard asked about
recently. It keeps a full book per token from `book` snapshots and
`price_change` updates, the same feed polymarket.com renders from, and
reconnects with the new token set when more tokens are wanted. The market
channel is public and read-only: it needs no key and cannot place orders.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import WebSocketException

from pmsim.data.models import OrderBookSnapshot
from pmsim.data.recorder import PING_INTERVAL_S, Connect, Socket, subscribe_frame

log = logging.getLogger(__name__)

WANT_TTL_S = 300.0  # drop tokens nobody has looked at for this long
MAX_TOKENS = 500
STALE_S = 30.0  # no frame (PONG included) for this long: books are not trusted
RESUBSCRIBE_S = 1.0  # at most one reconnect per second for new tokens
RECV_TIMEOUT_S = 1.0
BACKOFF_S = 1.0  # first reconnect delay, doubling to 30 s
SPREAD_CUTOFF = 0.10  # Polymarket shows the last trade instead of the midpoint above this

Top = tuple[float | None, float | None]  # best bid, best ask


@dataclass
class _Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    tick: float | None = None
    ts_ms: int = 0


def displayed_price(bid: float | None, ask: float | None, last: float | None) -> float:
    """The price polymarket.com shows: the midpoint, or the last trade when the spread is wide.

    An empty side counts as its bound (bid 0, ask 1), so a one-sided book has a wide spread.
    """
    lo = 0.0 if bid is None else bid
    hi = 1.0 if ask is None else ask
    if hi - lo > SPREAD_CUTOFF and last is not None:
        return last
    return (lo + hi) / 2


class LiveStream:
    """Thread-safe live books for the tokens passed to `watch`."""

    def __init__(
        self, url: str, connect: Connect, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._url = url
        self._connect = connect
        self._clock = clock
        self._lock = threading.Lock()
        self._wanted: dict[str, float] = {}
        self._books: dict[str, _Book] = {}
        self._subscribed: frozenset[str] = frozenset()
        self._subscribed_at = float("-inf")
        self._last_frame = float("-inf")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- public, called from request threads ----------------------------------------

    def watch(self, tokens: Iterable[str]) -> None:
        now = self._clock()
        with self._lock:
            for token in tokens:
                if token:
                    self._wanted[token] = now
        if self._thread is None:
            self._thread = threading.Thread(target=self._thread_main, name="pm-stream", daemon=True)
            self._thread.start()

    def top(self, token: str) -> Top | None:
        """Best bid and ask, or None when the stream has no live book for `token`."""
        with self._lock:
            book = self._live_book(token)
            if book is None:
                return None
            return max(book.bids, default=None), min(book.asks, default=None)

    def snapshot(self, token: str) -> OrderBookSnapshot | None:
        with self._lock:
            book = self._live_book(token)
            if book is None:
                return None
            raw = {
                "market": "",
                "asset_id": token,
                "timestamp": book.ts_ms,
                "hash": "",
                "bids": [{"price": p, "size": s} for p, s in book.bids.items()],
                "asks": [{"price": p, "size": s} for p, s in book.asks.items()],
                "tick_size": book.tick,
            }
        return OrderBookSnapshot.model_validate(raw)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ---- frame handling -----------------------------------------------------------------

    def apply(self, raw: str) -> None:
        """Fold one WebSocket frame (an event, a list of events, or PONG) into the books."""
        with self._lock:
            self._last_frame = self._clock()
            if not raw.lstrip().startswith(("{", "[")):
                return
            decoded = json.loads(raw)
            for ev in decoded if isinstance(decoded, list) else [decoded]:
                self._apply_event(ev)

    def _apply_event(self, ev: Mapping[str, Any]) -> None:
        kind = ev.get("event_type")
        ts = int(ev.get("timestamp") or 0)
        if kind == "book":
            self._books[ev["asset_id"]] = _Book(
                bids={float(lv["price"]): float(lv["size"]) for lv in ev["bids"]},
                asks={float(lv["price"]): float(lv["size"]) for lv in ev["asks"]},
                tick=float(ev["tick_size"]) if ev.get("tick_size") else None,
                ts_ms=ts,
            )
        elif kind == "price_change":
            for ch in ev["price_changes"]:
                self._apply_change(ch, ts)
        elif kind == "tick_size_change" and (book := self._books.get(ev["asset_id"])):
            book.tick = float(ev["new_tick_size"])

    def _apply_change(self, ch: Mapping[str, Any], ts: int) -> None:
        book = self._books.get(ch["asset_id"])
        if book is None:
            return
        levels = book.bids if ch["side"] == "BUY" else book.asks
        price, size = float(ch["price"]), float(ch["size"])
        if size > 0:
            levels[price] = size
        else:
            levels.pop(price, None)
        book.ts_ms = max(book.ts_ms, ts)

    def _live_book(self, token: str) -> _Book | None:
        if self._clock() - self._last_frame > STALE_S or token not in self._subscribed:
            return None
        return self._books.get(token)

    # ---- connection -------------------------------------------------------------------------

    def _targets(self) -> frozenset[str]:
        with self._lock:
            cutoff = self._clock() - WANT_TTL_S
            self._wanted = {t: at for t, at in self._wanted.items() if at >= cutoff}
            recent = sorted(self._wanted, key=self._wanted.__getitem__, reverse=True)
            return frozenset(recent[:MAX_TOKENS])

    def _needs_resubscribe(self) -> bool:
        with self._lock:
            if self._clock() - self._subscribed_at < RESUBSCRIBE_S:
                return False
        return not self._targets() <= self._subscribed

    def _thread_main(self) -> None:
        asyncio.run(self.run())

    async def run(self) -> None:
        """Stay connected until `close`, resubscribing as the wanted tokens grow."""
        failures = 0
        while not self._stop.is_set():
            tokens = self._targets()
            if not tokens:
                await asyncio.sleep(RECV_TIMEOUT_S)
                continue
            try:
                await self._session(tokens)
                failures = 0
            except (OSError, WebSocketException, TimeoutError) as exc:
                failures += 1
                log.warning("market stream dropped (%r); retry %d", exc, failures)
                with self._lock:
                    self._books.clear()
                await asyncio.sleep(min(BACKOFF_S * 2 ** (failures - 1), 30.0))

    async def _session(self, tokens: frozenset[str]) -> None:
        async with self._connect(self._url) as ws:
            await ws.send(subscribe_frame(sorted(tokens)))
            with self._lock:
                self._subscribed = tokens
                self._subscribed_at = self._last_frame = self._clock()
            ping = asyncio.create_task(_keepalive(ws))
            try:
                await self._read(ws)
            finally:
                ping.cancel()

    async def _read(self, ws: Socket) -> None:
        while not self._stop.is_set() and not self._needs_resubscribe():
            try:
                raw = await asyncio.wait_for(ws.recv(), RECV_TIMEOUT_S)
            except TimeoutError:
                with self._lock:
                    quiet = self._clock() - self._last_frame > STALE_S
                if quiet:
                    raise
                continue
            self.apply(raw.decode() if isinstance(raw, bytes) else raw)


async def _keepalive(ws: Socket) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_S)
        await ws.send("PING")
