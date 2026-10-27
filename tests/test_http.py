from pathlib import Path

import httpx
import pytest

from pmsim.config import ApiConfig
from pmsim.data.http import HttpClient, HttpError, JsonCache, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s


def test_rate_limiter_spaces_calls() -> None:
    clock = FakeClock()
    rl = RateLimiter(2.0, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        rl.acquire()
    assert clock.slept == pytest.approx([0.5, 0.5])


def test_rate_limiter_no_wait_after_idle() -> None:
    clock = FakeClock()
    rl = RateLimiter(1.0, clock=clock, sleep=clock.sleep)
    rl.acquire()
    clock.now += 5
    rl.acquire()
    assert clock.slept == []


def _client(statuses: list[int], **api: float) -> tuple[HttpClient, list[float], list[int]]:
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        code = statuses[min(len(calls), len(statuses) - 1)]
        calls.append(code)
        return httpx.Response(code, json={"ok": True})

    cfg = ApiConfig(requests_per_second=1e9, backoff_s=0.5, **api)  # type: ignore[arg-type]
    return (
        HttpClient(cfg, transport=httpx.MockTransport(handler), sleep=sleeps.append),
        sleeps,
        calls,
    )


def test_retries_then_succeeds_with_backoff() -> None:
    client, sleeps, calls = _client([429, 503, 200])
    assert client.get_json("https://x/a") == {"ok": True}
    assert calls == [429, 503, 200]
    assert [s for s in sleeps if s >= 0.5] == [0.5, 1.0]


def test_gives_up_after_max_retries() -> None:
    client, _, calls = _client([500], max_retries=2)
    with pytest.raises(HttpError, match="3 tries"):
        client.get_json("https://x/a")
    assert len(calls) == 3


def test_client_error_not_retried() -> None:
    client, _, calls = _client([404])
    with pytest.raises(HttpError, match="404"):
        client.get_json("https://x/a")
    assert calls == [404]


def test_transport_error_retried() -> None:
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[1])

    cfg = ApiConfig(requests_per_second=1e9, backoff_s=0)
    client = HttpClient(cfg, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    assert client.get_json("https://x/a") == [1]


def test_cache_hit_skips_network(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(200, json={"v": len(calls)})

    cfg = ApiConfig(requests_per_second=1e9)
    cache = JsonCache(tmp_path, ttl_s=60)
    client = HttpClient(cfg, cache=cache, transport=httpx.MockTransport(handler))
    assert client.get_json("https://x/a", {"q": 1}) == {"v": 1}
    assert client.get_json("https://x/a", {"q": 1}) == {"v": 1}
    assert client.get_json("https://x/a", {"q": 2}) == {"v": 2}
    assert len(calls) == 2


def test_cache_expires(tmp_path: Path) -> None:
    now = {"t": 0.0}
    cache = JsonCache(tmp_path, ttl_s=10, clock=lambda: now["t"])
    cache.put("u", {}, {"a": 1})
    path = next(tmp_path.iterdir())
    now["t"] = path.stat().st_mtime + 5
    assert cache.get("u", {}) == {"a": 1}
    now["t"] = path.stat().st_mtime + 11
    assert cache.get("u", {}) is None


def test_cache_disabled_with_zero_ttl(tmp_path: Path) -> None:
    cache = JsonCache(tmp_path, ttl_s=0)
    cache.put("u", {}, 1)
    assert cache.get("u", {}) is None
    assert list(tmp_path.iterdir()) == []


def test_client_has_no_post_method() -> None:
    assert not hasattr(HttpClient, "post")
