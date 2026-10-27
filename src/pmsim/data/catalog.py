"""Market catalog: one row per outcome token, stored as Parquet."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import polars as pl

from pmsim.data.models import Market

CATALOG_FILE = "catalog.parquet"

SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "market_id": pl.Utf8,
    "condition_id": pl.Utf8,
    "slug": pl.Utf8,
    "question": pl.Utf8,
    "token_id": pl.Utf8,
    "outcome": pl.Utf8,
    "outcome_index": pl.Int32,
    "event_id": pl.Utf8,
    "neg_risk": pl.Boolean,
    "neg_risk_market_id": pl.Utf8,
    "neg_risk_other": pl.Boolean,
    "group_item_title": pl.Utf8,
    "tick_size": pl.Float64,
    "min_order_size": pl.Float64,
    "fees_enabled": pl.Boolean,
    "fee_type": pl.Utf8,
    "fee_rate": pl.Float64,
    "fee_exponent": pl.Float64,
    "fee_taker_only": pl.Boolean,
    "status": pl.Utf8,
    "end_date": pl.Datetime("us", "UTC"),
    "tags": pl.List(pl.Utf8),
    "resolution_source": pl.Utf8,
    "rules": pl.Utf8,
}


def market_status(m: Market) -> str:
    """Collapse Gamma's flags into one status string."""
    if m.closed:
        return "closed"
    if m.active and m.accepting_orders:
        return "open"
    return "inactive"


def catalog_rows(m: Market) -> list[dict[str, object]]:
    """Expand one market into a row per outcome token."""
    shared = {
        "market_id": m.id,
        "condition_id": m.condition_id,
        "slug": m.slug,
        "question": m.question,
        "event_id": m.event_id,
        "neg_risk": m.neg_risk,
        "neg_risk_market_id": m.neg_risk_market_id,
        "neg_risk_other": m.neg_risk_other,
        "group_item_title": m.group_item_title,
        "tick_size": m.tick_size,
        "min_order_size": m.min_order_size,
        "fees_enabled": m.fees_enabled,
        "fee_type": m.fee_type,
        "fee_rate": m.fee_schedule.rate,
        "fee_exponent": m.fee_schedule.exponent,
        "fee_taker_only": m.fee_schedule.taker_only,
        "status": market_status(m),
        "end_date": m.end_date,
        "tags": [t.slug for t in m.tags],
        "resolution_source": m.resolution_source,
        "rules": m.rules,
    }
    return [
        {**shared, "token_id": tok, "outcome": out, "outcome_index": i}
        for i, (tok, out) in enumerate(zip(m.token_ids, m.outcomes, strict=True))
    ]


def build_catalog(markets: Iterable[Market]) -> pl.DataFrame:
    rows = [row for m in markets for row in catalog_rows(m)]
    return pl.DataFrame(rows, schema=SCHEMA).select(list(SCHEMA))


def save_catalog(df: pl.DataFrame, data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / CATALOG_FILE
    df.write_parquet(path)
    return path


def load_catalog(data_dir: Path) -> pl.DataFrame:
    return pl.read_parquet(data_dir / CATALOG_FILE)


def select_markets(df: pl.DataFrame, spec: str) -> pl.DataFrame:
    """Filter rows. `spec` is comma separated: `tag:<slug>`, `neg_risk`, or a slug substring.

    Terms are OR-ed. Only open markets are returned.
    """
    terms = [t.strip() for t in spec.split(",") if t.strip()]
    if not terms:
        raise ValueError("empty market filter")
    expr = pl.lit(False)
    for term in terms:
        expr = expr | _term_expr(term)
    return df.filter(expr & (pl.col("status") == "open"))


def _term_expr(term: str) -> pl.Expr:
    if term.startswith("tag:"):
        return pl.col("tags").list.contains(term.removeprefix("tag:"))
    if term == "neg_risk":
        return pl.col("neg_risk")
    return pl.col("slug").str.contains(term, literal=True)
