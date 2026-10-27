"""Loading your own algorithms from the algorithms folder."""

from pathlib import Path

import pytest

from pmsim.data.synthetic import SyntheticConfig, generate
from pmsim.engine.backtest import Backtest
from pmsim.strategies import custom
from pmsim.ui import api

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def test_example_algorithm_loads_and_trades() -> None:
    algos = custom.discover(ROOT / "algorithms")
    example = next(a for a in algos if a.name == "example_momentum")
    assert example.error == "" and example.title == "Momentum (example)"
    assert example.config is not None and "window" in example.config.model_fields
    strategy = custom.build(example, {"window": 4, "jump_cents": 1})
    assert strategy.name == "example_momentum"
    res = Backtest(strategy, generate(SyntheticConfig(duration_hours=2))).run()
    assert res.strategy == "example_momentum" and res.fills


def test_bad_files_report_errors(tmp_path: Path) -> None:
    write(tmp_path, "trader.py", "import web3\n")
    write(tmp_path, "broken.py", "def (:\n")
    write(tmp_path, "empty.py", "x = 1\n")
    write(tmp_path, "crash.py", "raise RuntimeError('boom')\n")
    write(tmp_path, "_private.py", "raise SystemExit\n")
    errors = {a.name: a.error for a in custom.discover(tmp_path)}
    assert set(errors) == {"trader", "broken", "empty", "crash"}
    assert "trading packages" in errors["trader"]
    assert "SyntaxError" in errors["broken"]
    assert "no Strategy" in errors["empty"]
    assert "boom" in errors["crash"]
    with pytest.raises(ValueError, match="failed to load"):
        custom.build(custom.discover(tmp_path)[0], {})
    assert custom.discover(tmp_path / "missing") == []


def test_config_free_algorithm(tmp_path: Path) -> None:
    write(
        tmp_path,
        "idle.py",
        "from pmsim.strategies.base import Strategy\n\n"
        "class Idle(Strategy):\n    '''Does nothing.'''\n",
    )
    algo = custom.discover(tmp_path)[0]
    assert algo.config is None and algo.blurb == "Does nothing."
    assert custom.build(algo, {}).name == "idle"


def test_dashboard_lists_and_runs_custom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pmsim.config import Config

    monkeypatch.setattr(custom, "ALGORITHMS_DIR", ROOT / "algorithms")
    names = {a["name"]: a for a in api.algorithms()}
    assert names["example_momentum"]["custom"] and names["example_momentum"]["params"]
    out = api.run(Config(data_dir=tmp_path), {"strategy": "example_momentum", "hours": 1})
    assert out["strategy"] == "example_momentum" and out["summary"]
    with pytest.raises(api.ApiError, match="window"):
        api.run(
            Config(data_dir=tmp_path), {"strategy": "example_momentum", "params": {"window": 1}}
        )
