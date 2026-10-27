# Decisions

One line per design choice: date, choice, reason.

- 2026-10-27: Python 3.12 with uv, DuckDB and Parquet storage, polars for analysis. Fast, local, no services.
- 2026-10-27: Simulator never signs or submits live orders. Safety first.
- 2026-10-27: hatchling build backend with src layout. Standard, no config needed.
- 2026-10-27: pydantic models with `extra="forbid"` for config. Typos in YAML fail loudly.
- 2026-10-27: argparse for the CLI, not click or typer. No extra dependency for a small CLI.
- 2026-10-27: Ruff rules E,F,I,B,UP plus mypy strict with the pydantic plugin. Strict typing from day one.
- 2026-10-27: Public trades come from data-api `/trades`; CLOB `/trades` needs L2 auth. Keeps the code key-free.
- 2026-10-27: Trade fixtures keep wallet and profile fields as "REDACTED". No personal data in the repo.
- 2026-10-27: Prices and sizes are float64 in the data layer and Parquet. Phase 2 converts to integer ticks for matching.
- 2026-10-27: Book snapshots are stored one row per level, numbered from the best price (level 0). CLOB returns bids ascending and asks descending, so we re-sort.
- 2026-10-27: Market catalog is one row per outcome token with status open/closed/inactive. Recording works per token.
- 2026-10-27: Default 5 requests per second, 4 retries with doubling backoff on 429/5xx and transport errors. Well below documented limits.
- 2026-10-27: GET cache on disk keyed by URL plus params, 300 s TTL. Repeated fetches during development stay off the network.
- 2026-10-27: Recorder sends the documented text `PING` every 10 s and reconnects with capped exponential backoff.
- 2026-10-27: `recv_ts_ms - exchange_ts_ms` was -5 to +2229 ms live. Clock skew is small but can be negative; latency modeling must not assume ordering.
- 2026-10-27: Fee docs read on 2026-10-27: taker fee = C * feeRate * p * (1 - p), makers pay none, rate by category (geopolitics 0). Catalog stores feeSchedule.rate per market.
