"""Dashboard API and server. The server runs on an ephemeral port in a thread."""

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from conftest import load_fixture, ws_frames
from pmsim.config import Config
from pmsim.data.catalog import build_catalog, save_catalog
from pmsim.data.models import Market
from pmsim.data.recorder import ParquetSink, parse_message
from pmsim.engine import state
from pmsim.engine.fills import Fill
from pmsim.engine.portfolio import Portfolio
from pmsim.strategies.complementary import ComplementaryConfig
from pmsim.strategies.registry import BUILDERS
from pmsim.ui import api
from pmsim.ui.server import make_server


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """A data dir with the fixture catalog, recorded frames and one paper session."""
    frames = ws_frames()
    tokens = sorted({json.loads(frames[0])[0]["asset_id"], json.loads(frames[0])[1]["asset_id"]})
    markets = []
    for j, raw in enumerate(load_fixture("gamma_markets.json")):
        ids = tokens if j == 0 else [f"x{j}a", f"x{j}b"]
        markets.append(Market.model_validate({**raw, "clobTokenIds": json.dumps(ids)}))
    save_catalog(build_catalog(markets), tmp_path)
    sink = ParquetSink(tmp_path / "raw")
    for i, frame in enumerate(frames):
        sink.add(parse_message(frame, 1_793_084_400_000 + i))
    sink.flush()
    pf = Portfolio(cash=10**14, now=1)
    pf.apply_fill(Fill(1, tokens[0], "BUY", 5000, 10**7, 2, "taker", 0, 0))
    state.save(pf, tmp_path / "paper" / "demo.state.json")
    (tmp_path / "paper" / "demo.log.jsonl").write_text(
        json.dumps({"ts": 1, "kind": "order", "detail": f"BUY 10 @ 50c {tokens[0]}", "reason": "r"})
        + "\n"
    )
    return Config(data_dir=tmp_path)


# ---- api functions --------------------------------------------------------------------


def test_meta_lists_strategies_and_data(cfg: Config) -> None:
    m = api.meta(cfg)
    names = [s["name"] for s in m["strategies"]]
    assert names[: len(BUILDERS)] == sorted(BUILDERS) and "example_momentum" in names
    comp = next(s for s in m["strategies"] if s["name"] == "complementary")
    edge = next(p for p in comp["params"] if p["name"] == "min_edge_usd_per_share")
    assert edge["type"] == "float" and edge["default"] == 0.005 and edge["ge"] == 0
    assert edge["title"] == "Minimum profit per share" and edge["help"]
    assert m["recorded"]["available"] and m["recorded"]["markets"] == 2
    assert m["stresses"][0] == "base"


def test_param_fields_skip_lists() -> None:
    names = {p["name"] for p in api.param_fields(ComplementaryConfig)}
    assert "leg_timeout_ms" in names
    from pmsim.strategies.logical import LogicalConfig

    assert "relations" not in {p["name"] for p in api.param_fields(LogicalConfig)}


def test_run_synthetic_with_stress(cfg: Config) -> None:
    out = api.run(cfg, {"strategy": "complementary", "hours": 1, "seed": 3, "stress": True})
    assert out["label"] == "synthetic" and out["data"] == {
        "kind": "synthetic",
        "hours": 1.0,
        "seed": 3,
    }
    assert [r["stress"] for r in out["stress"]["rows"]] == [
        "base",
        "latency_x3",
        "depth_x0.5",
        "fees_x2",
    ]
    assert out["equity"] and len(out["equity"]) <= api.MAX_POINTS + 1
    assert out["equity"][0][0] == 0
    assert out["metrics"]["net_pnl_usd"] == out["stress"]["rows"][0]["net_pnl"]
    assert json.dumps(out)


def test_run_recorded_labels_tokens(cfg: Config) -> None:
    out = api.run(cfg, {"strategy": "market_making", "data": "recorded"})
    assert out["label"] == "recorded" and out["stress"] is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"strategy": "nope"}, "unknown strategy"),
        ({"strategy": "complementary", "hours": 999}, "hours must be between"),
        ({"strategy": "complementary", "hours": "x"}, "hours must be a number"),
        ({"strategy": "complementary", "data": "live"}, "data must be"),
        ({"strategy": "complementary", "params": []}, "params must be an object"),
        ({"strategy": "complementary", "params": {"max_shares": -1}}, "max_shares"),
        ({"strategy": "complementary", "params": {"bogus": 1}}, "bogus"),
    ],
)
def test_run_rejects_bad_input(cfg: Config, body: dict[str, object], message: str) -> None:
    with pytest.raises(api.ApiError, match=message):
        api.run(cfg, body)


def test_recorded_requires_data(tmp_path: Path) -> None:
    with pytest.raises(api.ApiError, match="no recorded data"):
        api.run(Config(data_dir=tmp_path), {"strategy": "complementary", "data": "recorded"})


def test_compare_all_strategies(cfg: Config) -> None:
    out = api.compare(cfg, {"hours": 0.5})
    assert [r["strategy"] for r in out["rows"]][: len(BUILDERS)] == sorted(BUILDERS)
    assert all(r["label"] == "synthetic" for r in out["rows"])


# ---- http server ---------------------------------------------------------------------------


@pytest.fixture
def client(cfg: Config) -> Iterator[httpx.Client]:
    server = make_server(cfg, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    with httpx.Client(base_url=f"http://{host}:{port}", timeout=60) as c:
        yield c
    server.shutdown()
    server.server_close()


def test_static_files_and_traversal(client: httpx.Client) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "polysim" in page.text
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/app.css").status_code == 200
    assert client.get("/../pyproject.toml").status_code == 404
    assert client.get("/nope.html").status_code == 404
    assert client.get("/server.py").status_code == 404


def test_api_routes(client: httpx.Client) -> None:
    assert client.get("/api/meta").json()["recorded"]["available"]
    assert client.get("/api/portfolio").json()["cash"] == 10_000
    assert client.get("/api/bots").json()["bots"] == []
    run = client.post("/api/run", json={"strategy": "logical", "hours": 0.5})
    assert run.status_code == 200 and run.json()["strategy"] == "logical"
    assert client.post("/api/compare", json={"hours": 0.25}).status_code == 200


def test_api_errors(client: httpx.Client) -> None:
    bad = client.post("/api/run", json={"strategy": "nope"})
    assert bad.status_code == 400 and "unknown strategy" in bad.json()["error"]
    json_type = {"Content-Type": "application/json"}
    assert client.post("/api/run", content=b"{not json", headers=json_type).status_code == 400
    assert client.post("/api/run", json=[1]).status_code == 400
    assert client.post("/api/other", json={}).status_code == 404
    assert client.post("/api/run", content=b"x" * 70_000, headers=json_type).status_code == 413


def test_request_guard(client: httpx.Client) -> None:
    """Other websites and non-loopback hosts cannot drive the dashboard."""
    assert client.post("/api/run", content=b"{}").status_code == 415  # form-style body
    evil = client.post("/api/run", json={}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    rebound = client.get("/api/meta", headers={"Host": "evil.example:8765"})
    assert rebound.status_code == 403
    host = client.base_url.netloc.decode()
    same = client.post("/api/run", json={"strategy": "nope"}, headers={"Origin": f"http://{host}"})
    assert same.status_code == 400
