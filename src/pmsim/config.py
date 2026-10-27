"""Typed loading of the single YAML config."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class FeeConfig(BaseModel):
    """Fee model parameters. `checked_on` records when the docs were last read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    taker_bps: float = Field(default=0.0, ge=0)
    maker_bps: float = Field(default=0.0, ge=0)
    gas_per_trade_usd: float = Field(default=0.0, ge=0)
    checked_on: date | None = None


class Config(BaseModel):
    """Top-level simulator configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path = Path("data")
    seed: int = 42
    latency_ms: int = Field(default=150, ge=0)
    fees: FeeConfig = FeeConfig()


def load_config(path: Path | str) -> Config:
    """Load and validate a YAML config file."""
    raw = yaml.safe_load(Path(path).read_text())
    return Config.model_validate(raw or {})
