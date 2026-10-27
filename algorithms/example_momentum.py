"""Example algorithm: copy this file to write your own.

Every `.py` file in this folder that defines a `Strategy` subclass shows up in
the dashboard under "Your algorithms". A nested pydantic `Config` becomes the
settings form (use `title` and `description` for labels and help). Orders you
return only ever reach the simulator.
"""

from collections import deque

from pydantic import BaseModel, Field

from pmsim.book.units import PRICE_SCALE, size_units
from pmsim.strategies.base import BookUpdate, Intent, OrderIntent, Strategy


class Momentum(Strategy):
    """Buys YES after the price climbs steadily, sells after it falls back.

    Keeps the last few mid prices per market. A rise of at least `jump` cents
    over that window triggers a buy; a fall of the same size sells everything.
    """

    title = "Momentum (example)"

    class Config(BaseModel):
        window: int = Field(
            default=12,
            ge=2,
            title="Window (updates)",
            description="How many book updates to look back.",
        )
        jump_cents: float = Field(
            default=3,
            gt=0,
            title="Move to act on (cents)",
            description="Price change that triggers a trade.",
        )
        shares: float = Field(
            default=20, gt=0, title="Shares per buy", description="Shares bought on each signal."
        )
        max_shares: float = Field(
            default=100,
            gt=0,
            title="Max shares held",
            description="Never hold more than this per market.",
        )

    def __init__(self, cfg: "Momentum.Config | None" = None) -> None:
        self.cfg = cfg or Momentum.Config()
        self.mids: dict[str, deque[float]] = {}

    def on_book(self, update: BookUpdate) -> list[Intent]:
        view, cfg, token = self.view, self.cfg, update.token
        meta = view.meta.get(token)
        book = view.book(token)
        if meta is None or meta.outcome_index != 0 or book is None or view.open_orders(token):
            return []
        mid = book.mid()
        if mid is None:
            return []
        history = self.mids.setdefault(token, deque(maxlen=cfg.window))
        history.append(mid)
        if len(history) < cfg.window:
            return []
        move = (history[-1] - history[0]) / PRICE_SCALE * 100
        held = view.position(token)
        if (
            move >= cfg.jump_cents
            and book.best_ask
            and held + size_units(cfg.shares) <= size_units(cfg.max_shares)
        ):
            price, depth = book.best_ask
            history.clear()
            return [
                OrderIntent(
                    token,
                    "BUY",
                    price,
                    min(depth, size_units(cfg.shares)),
                    "IOC",
                    tag="momentum up",
                )
            ]
        if move <= -cfg.jump_cents and held and book.best_bid:
            price, depth = book.best_bid
            history.clear()
            return [OrderIntent(token, "SELL", price, min(depth, held), "IOC", tag="momentum down")]
        return []
