from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import load_fixture
from pmsim.cli import main as cli
from pmsim.cli.main import main
from pmsim.data.models import Market


def test_main_no_args_ok() -> None:
    assert main([]) == 0


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "pmsim" in capsys.readouterr().out


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "c.yaml"
    p.write_text(f"data_dir: {tmp_path / 'data'}\n")
    return p


def _fake_iter(self: Any, max_markets: int, **_: Any) -> Iterator[Market]:
    for m in load_fixture("gamma_markets.json")[:max_markets]:
        yield Market.model_validate(m)


def test_fetch_writes_catalog(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.GammaClient, "iter_markets", _fake_iter)
    assert main(["--config", str(cfg_path), "fetch", "--limit", "5"]) == 0
    assert (cfg_path.parent / "data" / "catalog.parquet").exists()
    assert "saved 2 markets" in capsys.readouterr().out


def test_record_needs_catalog(cfg_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(cfg_path), "record", "--markets", "x", "--hours", "1"]) == 2
    assert "pmsim fetch" in capsys.readouterr().err


def test_record_selects_tokens(cfg_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.GammaClient, "iter_markets", _fake_iter)
    main(["--config", str(cfg_path), "fetch"])
    calls: list[tuple[list[str], float]] = []

    async def fake_record(
        connect: Any, url: str, tokens: list[str], secs: float, sink: Any
    ) -> None:
        calls.append((tokens, secs))

    monkeypatch.setattr(cli, "record", fake_record)
    argv = ["--config", str(cfg_path), "record", "--markets", "xi-jinping", "--hours", "0.5"]
    assert main(argv) == 0
    assert len(calls[0][0]) == 2 and calls[0][1] == 1800
    argv[4] = "nothing-matches"
    assert main(argv) == 2
