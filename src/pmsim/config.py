"""Typed loading of the single YAML config."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class FeeConfig(BaseModel):
    """Fee model parameters. `checked_on` records when the docs were last read.

    Taker fee per fill is C * rate * p * (1 - p) (docs, checked 2026-10-27).
    Each market's own rate from the catalog is used when it has a schedule.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_taker_rate: float = Field(default=0.05, ge=0)
    maker_rate: float = Field(default=0.0, ge=0)
    multiplier: float = Field(default=1.0, ge=0)
    gas_per_trade_usd: float = Field(default=0.0, ge=0)
    checked_on: date | None = None


class ApiConfig(BaseModel):
    """Endpoints and client behavior for the public, read-only APIs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    data_url: str = "https://data-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    requests_per_second: float = Field(default=5.0, gt=0)
    max_retries: int = Field(default=4, ge=0)
    backoff_s: float = Field(default=0.5, ge=0)
    timeout_s: float = Field(default=15.0, gt=0)
    cache_ttl_s: float = Field(default=300.0, ge=0)


class Config(BaseModel):
    """Top-level simulator configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path = Path("data")
    seed: int = 42
    latency_ms: int = Field(default=150, ge=0)
    fees: FeeConfig = FeeConfig()
    api: ApiConfig = ApiConfig()


def load_config(path: Path | str) -> Config:
    """Load and validate a YAML config file."""
    raw = yaml.safe_load(Path(path).read_text())
    return Config.model_validate(raw or {})
