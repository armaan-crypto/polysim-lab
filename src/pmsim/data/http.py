"""Read-only HTTP plumbing: rate limiting, retries with backoff, local cache."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from pmsim.config import ApiConfig

JsonValue = Any
Params = Mapping[str, str | int | float | bool]

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class RateLimiter:
    """Token bucket. `clock` and `sleep` are injectable for tests."""

    def __init__(
        self,
        rate_per_s: float,
        burst: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = rate_per_s
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def acquire(self) -> None:
        """Block until one token is available, then take it."""
        self._refill()
        if self._tokens < 1.0:
            self._sleep((1.0 - self._tokens) / self._rate)
            self._refill()
        self._tokens -= 1.0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now


class JsonCache:
    """On-disk cache of GET responses keyed by URL and params, with a TTL."""

    def __init__(self, root: Path, ttl_s: float, clock: Callable[[], float] = time.time) -> None:
        self._root = root
        self._ttl = ttl_s
        self._clock = clock

    def get(self, url: str, params: Params) -> JsonValue | None:
        path = self._path(url, params)
        if self._ttl <= 0 or not path.exists():
            return None
        if self._clock() - path.stat().st_mtime > self._ttl:
            return None
        return json.loads(path.read_text())

    def put(self, url: str, params: Params, value: JsonValue) -> None:
        if self._ttl <= 0:
            return
        path = self._path(url, params)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def _path(self, url: str, params: Params) -> Path:
        key = json.dumps([url, sorted(params.items())], default=str)
        return self._root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


class HttpError(RuntimeError):
    """Raised when a request fails after all retries."""


class HttpClient:
    """GET-only JSON client. There is deliberately no POST method."""

    def __init__(
        self,
        api: ApiConfig,
        cache: JsonCache | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(timeout=api.timeout_s, transport=transport)
        self._limiter = RateLimiter(api.requests_per_second, sleep=sleep)
        self._max_retries = api.max_retries
        self._backoff = api.backoff_s
        self._cache = cache
        self._sleep = sleep

    def get_json(self, url: str, params: Params | None = None) -> JsonValue:
        """GET `url` and decode JSON, using the cache when fresh."""
        query: Params = params or {}
        if self._cache is not None and (hit := self._cache.get(url, query)) is not None:
            return hit
        value = self._get_with_retries(url, query)
        if self._cache is not None:
            self._cache.put(url, query, value)
        return value

    def close(self) -> None:
        self._client.close()

    def _get_with_retries(self, url: str, params: Params) -> JsonValue:
        last_error = ""
        for attempt in range(self._max_retries + 1):
            if attempt:
                self._sleep(self._backoff * 2 ** (attempt - 1))
            self._limiter.acquire()
            try:
                resp = self._client.get(url, params=dict(params))
            except httpx.TransportError as exc:
                last_error = repr(exc)
                continue
            if resp.status_code in RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}"
                continue
            if resp.is_error:
                raise HttpError(f"GET {url} -> HTTP {resp.status_code}")
            return resp.json()
        raise HttpError(f"GET {url} failed after {self._max_retries + 1} tries: {last_error}")
