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
- 2026-10-27: Integer units: price 1e-4, size 1e-6 shares, cash = price * size (1 USDC = 1e10). Fills and conservation checks are exact.
- 2026-10-27: Fees rounded to whole micro-USDC per fill. The docs give no rounding rule; USDC has 6 decimals.
- 2026-10-27: All 500 live fee schedules have exponent 1, so the documented formula C * rate * p * (1 - p) is used as is. Markets with no schedule (null feeType) use `default_taker_rate` 0.05, the conservative "Other / General" rate.
- 2026-10-27: Gas or settlement cost is charged per fill (`gas_per_trade_usd`). Conservative for orders that fill in pieces.
- 2026-10-27: L2 replay uncross rule: the newest level wins and crossed opposite levels are dropped. Feed deltas for the two sides can arrive out of order.
- 2026-10-27: The feed reports best_ask 1 and best_bid 0 for an empty side. Replay of a live 20 s recording matched the feed's best prices on all 160 tokens under that convention.
- 2026-10-27: Same-timestamp ordering: snapshot, tick change, trade, delta. Trades come before the deltas they cause.
- 2026-10-27: Requests reach the exchange at submit + latency and run after market events stamped at the same time. Slightly pessimistic.
- 2026-10-27: Taken depth stays consumed until the recorded level drops by that much or a snapshot arrives. Stops the sim from taking the same liquidity twice.
- 2026-10-27: Resting orders fill only from trade prints at or through their price, after the queue ahead. Queue ahead starts at the visible level size and only shrinks with trades or when the level shrinks below it. The WS trade `side` is not used: the docs do not say whose side it is.
- 2026-10-27: Orders that would cross our own resting orders are rejected. No self-trades, and our orders never cross.
- 2026-10-27: Validation happens at exchange arrival time, so a tick-size change in flight applies.
