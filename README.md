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
