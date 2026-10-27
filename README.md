# polysim-lab

A simulator and backtester for testing trading algorithms on Polymarket
prediction markets. It never places real orders and holds no private keys;
tests fail if any trading or signing code appears.

Status is tracked in PROGRESS.md, design choices in DECISIONS.md, and the
build plan in docs/BUILD_SPEC.md.

## Architecture

```mermaid
flowchart LR
    subgraph data[pmsim.data]
        G[Gamma client] --> C[(Catalog)]
        K[CLOB + data-api clients] --> C
        W[WebSocket feed] --> R[Recorder]
        R --> P[(Parquet)]
        SY[Synthetic generator]
        KA[Kalshi client + mock feed]
    end
    P --> B[pmsim.book<br/>L2 / L3 books, replay]
    SY --> BT
    B --> BT[pmsim.engine<br/>backtest loop, fills, fees,<br/>portfolio, experiments]
    BT --> S[pmsim.strategies<br/>six strategies, legs,<br/>lookahead guard]
    S -->|order intents| BT
    KA --> S
    BT --> RK[pmsim.risk<br/>resolution, neg-risk]
    BT --> A[pmsim.analysis<br/>metrics, HTML report]
    W -.paper mode.-> BT
```

## Dashboard

```
uv run pmsim ui          # opens http://127.0.0.1:8765 (localhost works too)
```

A local web app that works like Polymarket, with paper money:

- **Markets**: every open Polymarket event, live from the public API, with
  chances, Yes/No prices and volume. Search and browse by category.
- **Market page**: price chart (1H to All), live order book, and a Buy/Sell
  panel with market and limit orders. Market orders fill against the real
  live book (you pay the real spread and depth); limit orders rest until the
  live price reaches them. Nothing is ever sent to Polymarket.
- **Portfolio**: cash, positions valued at live prices, P&L, open orders and
  trade history. Positions settle automatically when a market resolves.
  "Reset paper account" starts over.
- **Bots**: build a bot without code. Pick a market and outcome, choose
  "Buy the dip" or "Ride a breakout", set entry, take-profit, stop-loss and
  size, backtest it on that market's real price history, then start it.
  Running bots trade the paper account while the dashboard is running.
- **Strategy lab**: run the six research algorithms (or your own) on
  simulated or recorded data with presets, stress tests and a plain-English
  verdict, or compare them all.

### Your own algorithms

Copy `algorithms/example_momentum.py`, edit it, and reload the dashboard: it
appears in the Strategy lab with a settings form built from its `Config`.
Files that import trading or signing libraries are refused.

Links like `#event/<slug>`, `#bots/new?event=<slug>`, `#lab?algo=logical&run=1`
or `#markets?q=fed` open a view directly. The server only answers on
localhost and rejects requests from other websites.

## How to run it

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```
uv sync          # install
make check       # ruff, mypy, pytest with an 85% coverage floor on engine and book
make bench       # time a 24 h synthetic replay for every strategy
```

### 1. Get market data (read-only)

```
uv run pmsim fetch --limit 500                                # catalog -> data/catalog.parquet
uv run pmsim record --markets "tag:fed,neg_risk" --hours 1    # books + trades -> data/raw/
```

`--markets` takes comma separated terms (OR-ed): a slug substring,
`tag:<slug>`, or `neg_risk`. Only open markets are recorded. Recorded tables
are `book_snapshots`, `book_deltas`, `trades` and `tick_size_changes`; every
row has `exchange_ts_ms` and `recv_ts_ms`.

### 2. Backtest

```
uv run pmsim backtest --strategy complementary                # synthetic data (default)
uv run pmsim backtest --strategy logical --stress --workers 4 # base + latency, depth, fee stress
uv run pmsim backtest --strategy market_making --data recorded --report reports/mm.html
uv run pmsim edges                                            # theoretical vs realized edge
```

Strategies: `complementary`, `negrisk_sum`, `near_resolution`, `logical`,
`market_making`, `cross_platform`. Pass parameters as JSON with
`--params '{"min_edge_usd_per_share": 0.01}'`. Every summary and report says
whether it ran on `synthetic` or `recorded` data. A strategy passes the
stress test only if it stays positive in the base run and under each stress.

Sweeps and walk-forward splits are in `pmsim.engine.experiment`
(`sweep`, `walk_forward`, `stress_test`, `run_many` with a process pool).

### 3. Paper trade

```
uv run pmsim paper --strategy market_making --markets "tag:fed" --hours 1
```

Runs the strategy on the live feed with the backtest fill model. Decisions
go to `data/paper/<session>.log.jsonl` with a reason each; the portfolio is
saved to `data/paper/<session>.state.json` and resumed on restart.

## Read before trusting a number

- docs/METRICS.md: what each metric means.
- docs/ASSUMPTIONS.md: fee model and its date, latency, queue model,
  resolution risk.
- docs/LIMITATIONS.md: what the simulator cannot predict. In the recorded
  sample YES and NO books mirror each other exactly, so single-market
  complementary arbitrage looks absent on Polymarket; synthetic profits come
  from mispricings the generator injects.

Configuration lives in `config/default.yaml` (`pmsim.config.load_config`).
