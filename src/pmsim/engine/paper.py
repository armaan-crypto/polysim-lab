"""Paper trading: run a strategy on the live market feed with the backtest fill model.

The live WebSocket feed goes through the recorder's parser, becomes market
events, and drives the same `Backtest` loop used for historical runs. Orders
only ever reach the simulated `FillEngine`. Every decision goes to a JSONL
log with its reason, and the portfolio is saved periodically and on exit.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import polars as pl

from pmsim.book.replay import READERS, MarketEvent, event_key
from pmsim.data.recorder import TABLES, Rows
from pmsim.engine import state
from pmsim.engine.backtest import Backtest

Clock = Callable[[], float]


def rows_to_events(rows: Rows) -> list[MarketEvent]:
    """Convert parsed feed rows to replay events, in replay order."""
    events: list[MarketEvent] = []
    for table, table_rows in rows.items():
        if table_rows:
            df = pl.DataFrame(table_rows, schema=dict(TABLES[table]))
            events.extend(READERS[table](df))
    events.sort(key=event_key)
    return events


class PaperSink:
    """A recorder sink that trades instead of writing Parquet.

    The recorder calls `add` for every frame (heartbeat replies included) and
    `flush` when it stops.
    """

    def __init__(
        self,
        bt: Backtest,
        log_path: Path,
        state_path: Path,
        save_every_s: float = 30.0,
        wall_ms: Clock = lambda: time.time() * 1000,
    ) -> None:
        self.bt = bt
        self.log_path = log_path
        self.state_path = state_path
        self.save_every_ms = save_every_s * 1000
        self.wall_ms = wall_ms
        self._next_timer = bt.clock.now
        self._next_equity = bt.clock.now
        self._next_save = bt.clock.now + self.save_every_ms
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, rows: Rows) -> None:
        now = int(self.wall_ms())
        self._run_timers(now)
        events = rows_to_events(rows)
        batch: list[MarketEvent] = []
        for ev in events:
            if batch and ev.ts != batch[0].ts:
                self.bt.process_batch(batch)
                batch = []
            batch.append(ev)
        if batch:
            self.bt.process_batch(batch)
        self._write_log()
        if now >= self._next_save:
            self.save()
            self._next_save = now + self.save_every_ms

    def flush(self) -> None:
        self._run_timers(int(self.wall_ms()))
        self._write_log()
        self.save()

    def save(self) -> None:
        state.save(self.bt.portfolio, self.state_path)

    def _run_timers(self, now: int) -> None:
        step = self.bt.cfg.timer_ms
        while self._next_timer <= now:
            self._next_equity = self.bt.process_timer(self._next_timer, self._next_equity)
            self._next_timer += step

    def _write_log(self) -> None:
        """Append new decisions to the log and drop them from memory."""
        if not self.bt.decisions:
            return
        with self.log_path.open("a") as fh:
            for d in self.bt.decisions:
                fh.write(json.dumps(asdict(d)) + "\n")
        self.bt.decisions.clear()
