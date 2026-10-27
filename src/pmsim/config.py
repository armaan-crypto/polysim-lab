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


class ResolutionRisk(BaseModel):
    """Resolution risk for one market class. Probabilities are assumptions, not measured.

    Timings follow the UMA flow in the docs (checked 2026-10-27): a 2 hour
    challenge period, and 4 to 6 days in total when disputed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    p_late_proposal: float = Field(default=0.10, ge=0, le=1)
    late_proposal_mean_hours: float = Field(default=24.0, ge=0)
    challenge_hours: float = Field(default=2.0, ge=0)
    p_dispute: float = Field(default=0.02, ge=0, le=1)
    dispute_extra_hours: float = Field(default=118.0, ge=0)
    p_reversal: float = Field(default=0.25, ge=0, le=1)  # given a dispute
    p_fifty_fifty: float = Field(default=0.002, ge=0, le=1)  # binary markets only
    p_void: float = Field(default=0.0, ge=0, le=1)


class ResolutionConfig(BaseModel):
    """Per-class risk profiles. A market's class is the first whose tags it carries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    classes: dict[str, list[str]] = Field(default_factory=dict)
    profiles: dict[str, ResolutionRisk] = Field(
        default_factory=lambda: {"default": ResolutionRisk()}
    )
    redeem_cost_usd: float = Field(default=0.0, ge=0)


class Config(BaseModel):
    """Top-level simulator configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path = Path("data")
    seed: int = 42
    latency_ms: int = Field(default=150, ge=0)
    fees: FeeConfig = FeeConfig()
    api: ApiConfig = ApiConfig()
    resolution: ResolutionConfig = ResolutionConfig()


def load_config(path: Path | str) -> Config:
    """Load and validate a YAML config file."""
    raw = yaml.safe_load(Path(path).read_text())
    return Config.model_validate(raw or {})
