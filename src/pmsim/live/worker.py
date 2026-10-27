"""Background loop for the live paper desk: fill resting orders, settle, run bots."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from pmsim.live.account import PaperAccount
from pmsim.live.bots import BotManager
from pmsim.live.data import LiveData, resolved_payouts

MATCH_EVERY_S = 5.0
SETTLE_EVERY_S = 60.0


class Desk:
    """Ties the account, live data and bots together; `step` does one round of work."""

    def __init__(
        self,
        account: PaperAccount,
        data: LiveData,
        bots: BotManager,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.account = account
        self.data = data
        self.bots = bots
        self.clock = clock
        self._next_match = 0.0
        self._next_settle = 0.0
        self.errors: list[str] = []

    def step(self) -> None:
        now = self.clock()
        if now >= self._next_match:
            self._next_match = now + MATCH_EVERY_S
            self.match_orders()
        if now >= self._next_settle:
            self._next_settle = now + SETTLE_EVERY_S
            self.settle_resolved()
        self.bots.tick()

    def match_orders(self) -> None:
        tokens = {o["token"] for o in self.account.snapshot({})["orders"]}
        for token in tokens:
            try:
                self.account.match(token, self.data.book(token))
            except Exception as exc:  # one bad book must not stop the desk
                self._error(f"matching {token[:10]}: {exc}")

    def settle_resolved(self) -> None:
        for token in self.account.open_tokens():
            inst = self.account.instruments.get(token)
            if inst is None:
                continue
            try:
                payouts = resolved_payouts(self.data.market_status(inst.market_slug))
            except Exception as exc:
                self._error(f"resolution check {inst.market_slug}: {exc}")
                continue
            if payouts and inst.outcome_index < len(payouts):
                self.account.settle(token, payouts[inst.outcome_index])

    def _error(self, msg: str) -> None:
        self.errors = (self.errors + [msg])[-20:]


def run_forever(desk: Desk, stop: threading.Event, every_s: float = 1.0) -> threading.Thread:
    def loop() -> None:
        while not stop.wait(every_s):
            desk.step()

    thread = threading.Thread(target=loop, name="pmsim-desk", daemon=True)
    thread.start()
    return thread
