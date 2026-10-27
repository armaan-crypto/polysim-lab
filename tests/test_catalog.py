from pathlib import Path

import polars as pl
import pytest

from conftest import load_fixture
from pmsim.data.catalog import (
    SCHEMA,
    build_catalog,
    load_catalog,
    market_status,
    save_catalog,
    select_markets,
)
from pmsim.data.models import Market


@pytest.fixture
def markets() -> list[Market]:
    return [Market.model_validate(m) for m in load_fixture("gamma_markets.json")]


def test_one_row_per_token(markets: list[Market]) -> None:
    df = build_catalog(markets)
    assert df.height == 4
    assert df.columns == list(SCHEMA)
    assert df["outcome_index"].to_list() == [0, 1, 0, 1]
    assert df.filter(pl.col("neg_risk")).height == 2


def test_status_and_fee_fields(markets: list[Market]) -> None:
    df = build_catalog(markets)
    assert set(df["status"]) == {"open"}
    assert df["fee_taker_only"].all()
    assert df["tick_size"].min() > 0


def test_status_flags(markets: list[Market]) -> None:
    m = markets[0]
    assert market_status(m.model_copy(update={"closed": True})) == "closed"
    assert market_status(m.model_copy(update={"accepting_orders": False})) == "inactive"


def test_save_load_roundtrip(markets: list[Market], tmp_path: Path) -> None:
    df = build_catalog(markets)
    save_catalog(df, tmp_path / "d")
    assert load_catalog(tmp_path / "d").equals(df)


def test_select_by_slug_tag_and_neg_risk(markets: list[Market]) -> None:
    df = build_catalog(markets)
    assert select_markets(df, "xi-jinping").height == 2
    assert select_markets(df, "neg_risk").height == 2
    tag = df["tags"][0][0]
    assert select_markets(df, f"tag:{tag}").height >= 2
    assert select_markets(df, "xi-jinping, neg_risk").height == 4
    assert select_markets(df, "no-such-market").height == 0


def test_select_excludes_closed(markets: list[Market]) -> None:
    df = build_catalog(markets).with_columns(pl.lit("closed").alias("status"))
    assert select_markets(df, "xi-jinping").height == 0


def test_empty_filter_rejected(markets: list[Market]) -> None:
    with pytest.raises(ValueError):
        select_markets(build_catalog(markets), " , ")
