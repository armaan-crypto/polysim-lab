# Build spec: Polymarket simulator and strategy backtester

Python 3.12 research tool. Package name `pmsim` under `src/` (this is a new
codebase; do not reuse any other repo).

## Stack

- Python 3.12, uv, pytest, ruff, mypy (strict on core modules)
- pydantic models, httpx and websockets for network
- DuckDB plus Parquet storage, polars for analysis
- matplotlib plus a static HTML report
- No paid services. No real keys. Never place a live order.

## Phase 0: Scaffold

- Layout: src/pmsim/{data,book,engine,strategies,risk,analysis,cli}
- pyproject.toml, Makefile with `make check` (ruff, mypy, pytest)
- Single YAML config with typed loading
- README with an architecture diagram in Mermaid

## Phase 1: Data layer

- Gamma API client: market metadata (slug, outcomes, end date, tags,
  resolution source, rules text).
- CLOB API client: order books, prices, trades, price history.
- WebSocket recorder: order book snapshots and deltas plus trades to Parquet,
  with exchange timestamps and local receive timestamps.
- Market catalog table: market id, condition id, token ids, neg-risk flag,
  tick size, minimum order size, fee fields, status.
- Rate limiting, retries with backoff, local cache.
- Fixtures from real responses for tests when the network allows it.
  Verify endpoint shapes from the official docs before coding.
- CLI: `pmsim fetch`, `pmsim record --markets <filter> --hours N`.

## Phase 2: Order book and matching engine

- Limit order book per outcome token with price-time priority.
- Replay mode: rebuild the book from recorded snapshots and deltas.
- Fill model:
  - marketable orders walk the book and pay real depth
  - resting orders fill only when recorded trades cross our price, with a
    queue position estimate
  - configurable latency (default 150 ms)
  - partial fills and cancel races
- Fee model as a config object. Read the current schedule from the docs and
  record the date checked. Gas or settlement cost is a per-trade parameter.
- Enforce tick size and minimum size.
- Property tests: book never crosses, quantities stay non-negative, cash plus
  position value is conserved across fills.

## Phase 3: Portfolio, resolution, accounting

- Cash, positions per token, average cost, realized and unrealized PnL.
- Resolution engine: pay $1 to winning tokens, $0 to losers at market end.
- Resolution risk simulation: probability of delay, dispute and reversal, set
  per market class. Allow 50/50 and voided outcomes.
- Capital lock-up tracking: annualized return per position, idle cash.
- Neg-risk (multi-outcome) markets: YES prices of mutually exclusive outcomes
  and conversion between NO and other YES positions.

## Phase 4: Strategy framework

- Interface: `on_book(update)`, `on_trade(trade)`, `on_timer(t)`,
  `on_resolution(event)`, each returning a list of order intents.
- Strategies see only data stamped before the current sim time. A lookahead
  guard raises if a strategy reads future data.
- Reference strategies, each with config and tests:
  1. Complementary arbitrage: buy YES and NO when the pair costs less than $1
     after costs. Edge = 1 - (P_YES + P_NO) - fees - gas.
  2. Multi-outcome sum arbitrage on neg-risk markets (sum of YES below or
     above 1).
  3. Near-resolution yield: buy at 0.95 to 0.99 close to resolution, with a
     rules-text risk filter.
  4. Logical arbitrage across related markets (subset and superset events).
  5. Market making with inventory limits and spread targets.
  6. Cross-platform spread with Kalshi (interface and mock data first, plus a
     resolution-rule mismatch score).
- Multi-leg execution: model leg risk when only one leg fills, and the unwind
  cost.

## Phase 5: Backtester and experiment runner

- Event-driven backtest over recorded data. Deterministic with a seed.
- Parameter sweeps, walk-forward splits, parallel runs.
- Synthetic data generator: random-walk books, flash gaps, stale quotes, thin
  depth, halted markets. Label all synthetic results as synthetic.
- Stress tests: raise latency, halve depth, double fees. A strategy passes
  only if it stays positive under mild stress.

## Phase 6: Metrics and reports

- Net PnL, return on capital, annualized return on locked capital, Sharpe,
  max drawdown, hit rate, average edge per trade, fill rate, leg-failure rate,
  slippage versus quoted edge, resolution-loss count.
- Compare theoretical edge to realized edge for every strategy.
- HTML report per run: equity curve, trade table, failure log.
- Use LaTeX for any math in docs.

## Phase 7: Paper trading mode

- Run strategies on the live WebSocket feed with the same fill model.
- Log every decision with a reason. Persist state so it restarts cleanly.
- Hard safeguard: no code path signs or submits a real order. A test fails if
  any trading endpoint is imported.

## Phase 8: Hardening

- Coverage above 85% on engine, book and portfolio modules.
- Profile a one-day replay. Target under 60 seconds.
- docs/ASSUMPTIONS.md: fee model date, latency, queue model limits.
- docs/LIMITATIONS.md: what the simulator cannot predict.
