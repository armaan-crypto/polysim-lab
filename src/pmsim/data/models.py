"""Typed views of public API responses. Field names follow recorded fixtures."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Side = Literal["BUY", "SELL"]


class _Api(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


def _json_list(value: object) -> object:
    """Gamma encodes some lists as JSON strings, e.g. '["Yes", "No"]'."""
    return json.loads(value) if isinstance(value, str) else value


class Tag(_Api):
    slug: str
    label: str = ""


class FeeSchedule(_Api):
    rate: float = 0.0
    exponent: float = 1.0
    taker_only: bool = Field(default=True, alias="takerOnly")
    rebate_rate: float = Field(default=0.0, alias="rebateRate")


class Market(_Api):
    """One Gamma market (a binary question with outcome tokens)."""

    id: str
    condition_id: str = Field(alias="conditionId")
    slug: str
    question: str
    outcomes: list[str]
    token_ids: list[str] = Field(alias="clobTokenIds")
    end_date: datetime | None = Field(default=None, alias="endDate")
    rules: str = Field(default="", alias="description")
    resolution_source: str = Field(default="", alias="resolutionSource")
    tags: list[Tag] = Field(default_factory=list)
    neg_risk: bool = Field(default=False, alias="negRisk")
    tick_size: float = Field(alias="orderPriceMinTickSize")
    min_order_size: float = Field(alias="orderMinSize")
    fees_enabled: bool = Field(default=False, alias="feesEnabled")
    fee_type: str = Field(default="", alias="feeType")
    fee_schedule: FeeSchedule = Field(default_factory=FeeSchedule, alias="feeSchedule")
    active: bool = False
    closed: bool = False
    accepting_orders: bool = Field(default=False, alias="acceptingOrders")

    _decode_lists = field_validator("outcomes", "token_ids", mode="before")(_json_list)


class BookLevel(_Api):
    price: float
    size: float


class OrderBookSnapshot(_Api):
    """CLOB `/book` response. Bids ascend and asks descend, so best levels are last."""

    market: str
    asset_id: str
    timestamp_ms: int = Field(alias="timestamp")
    hash: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    tick_size: float | None = None
    min_order_size: float | None = None
    neg_risk: bool | None = None
    last_trade_price: float | None = None


class PricePoint(_Api):
    t: int
    p: float


class PublicTrade(_Api):
    """A trade from the data API `/trades` endpoint (wallet fields are not kept)."""

    side: Side
    asset: str
    condition_id: str = Field(alias="conditionId")
    size: float
    price: float
    timestamp: int
    outcome: str
    outcome_index: int = Field(alias="outcomeIndex")
    transaction_hash: str = Field(alias="transactionHash")
