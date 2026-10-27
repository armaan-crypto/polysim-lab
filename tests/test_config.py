from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from pmsim.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_default_config_loads() -> None:
    cfg = load_config(ROOT / "config" / "default.yaml")
    assert cfg.latency_ms == 150
    assert cfg.seed == 42


def test_empty_file_gives_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("")
    assert load_config(p) == Config()


def test_fee_date_parsed(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("fees:\n  checked_on: 2026-10-27\n")
    assert load_config(p).fees.checked_on == date(2026, 10, 27)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("bogus: 1\n")
    with pytest.raises(ValidationError):
        load_config(p)


def test_negative_latency_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("latency_ms: -1\n")
    with pytest.raises(ValidationError):
        load_config(p)
