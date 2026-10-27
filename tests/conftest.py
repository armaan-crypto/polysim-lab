"""Shared test helpers. Fixtures under tests/fixtures are recorded real responses."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from pmsim.config import ApiConfig
from pmsim.data.http import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def ws_frames() -> list[str]:
    return (FIXTURES / "ws_market.jsonl").read_text().splitlines()


Routes = dict[str, Any]


@pytest.fixture
def make_http() -> Callable[[Routes], tuple[HttpClient, list[httpx.Request]]]:
    """Build an HttpClient whose transport answers by URL path from `routes`."""

    def build(routes: Routes) -> tuple[HttpClient, list[httpx.Request]]:
        seen: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            if req.url.path not in routes:
                return httpx.Response(404)
            return httpx.Response(200, json=routes[req.url.path])

        api = ApiConfig(requests_per_second=1e9, backoff_s=0)
        return HttpClient(api, transport=httpx.MockTransport(handler), sleep=lambda _: None), seen

    return build
