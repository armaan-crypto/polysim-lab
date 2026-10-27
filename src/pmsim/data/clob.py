"""CLOB read-only client (books, prices, history) and public trades client."""

from __future__ import annotations

from typing import Literal

from pmsim.data.http import HttpClient, Params
from pmsim.data.models import OrderBookSnapshot, PricePoint, PublicTrade, Side

Interval = Literal["1h", "6h", "1d", "1w", "1m", "max"]


class ClobClient:
    """Public market-data endpoints of clob.polymarket.com. No trading endpoints."""

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def book(self, token_id: str) -> OrderBookSnapshot:
        raw = self._http.get_json(f"{self._base}/book", {"token_id": token_id})
        return OrderBookSnapshot.model_validate(raw)

    def price(self, token_id: str, side: Side) -> float:
        raw = self._http.get_json(f"{self._base}/price", {"token_id": token_id, "side": side})
        return float(raw["price"])

    def midpoint(self, token_id: str) -> float:
        raw = self._http.get_json(f"{self._base}/midpoint", {"token_id": token_id})
        return float(raw["mid"])

    def last_trade_price(self, token_id: str) -> tuple[float, Side]:
        raw = self._http.get_json(f"{self._base}/last-trade-price", {"token_id": token_id})
        return float(raw["price"]), raw["side"]

    def price_history(
        self,
        token_id: str,
        interval: Interval | None = "1d",
        fidelity_min: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[PricePoint]:
        """Use either `interval` or a `start_ts`/`end_ts` window (unix seconds)."""
        params: dict[str, str | int | float | bool] = {
            "market": token_id,
            "fidelity": fidelity_min,
        }
        if start_ts is not None and end_ts is not None:
            params.update(startTs=start_ts, endTs=end_ts)
        elif interval is not None:
            params["interval"] = interval
        raw = self._http.get_json(f"{self._base}/prices-history", params)
        return [PricePoint.model_validate(p) for p in raw["history"]]


class DataApiClient:
    """Public trades from data-api.polymarket.com (CLOB `/trades` needs auth)."""

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def trades(self, condition_id: str, limit: int = 100, offset: int = 0) -> list[PublicTrade]:
        params: Params = {"market": condition_id, "limit": limit, "offset": offset}
        raw = self._http.get_json(f"{self._base}/trades", params)
        return [PublicTrade.model_validate(t) for t in raw]
