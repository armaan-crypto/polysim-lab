"""Gamma API client: market metadata."""

from __future__ import annotations

from collections.abc import Iterator

from pmsim.data.http import HttpClient, Params
from pmsim.data.models import Market

PAGE_SIZE = 100


class GammaClient:
    """Read-only client for gamma-api.polymarket.com."""

    def __init__(self, http: HttpClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def list_markets(
        self,
        limit: int = PAGE_SIZE,
        offset: int = 0,
        active: bool | None = True,
        closed: bool | None = False,
        order: str | None = "volume24hr",
    ) -> list[Market]:
        """One page of markets, newest tags included."""
        params: dict[str, str | int | float | bool] = {
            "limit": limit,
            "offset": offset,
            "include_tag": "true",
        }
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if order:
            params["order"] = order
            params["ascending"] = "false"
        return self._markets(params)

    def iter_markets(
        self, max_markets: int, active: bool | None = True, closed: bool | None = False
    ) -> Iterator[Market]:
        """Page through markets until `max_markets` or the end of the list."""
        offset = 0
        while offset < max_markets:
            limit = min(PAGE_SIZE, max_markets - offset)
            page = self.list_markets(limit=limit, offset=offset, active=active, closed=closed)
            yield from page
            if len(page) < limit:
                return
            offset += limit

    def market_by_slug(self, slug: str) -> Market | None:
        found = self._markets({"slug": slug, "include_tag": "true"})
        return found[0] if found else None

    def _markets(self, params: Params) -> list[Market]:
        raw = self._http.get_json(f"{self._base}/markets", params)
        return [Market.model_validate(m) for m in raw if m.get("clobTokenIds")]
