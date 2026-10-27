# polysim-lab

A simulator and backtester for testing trading algorithms on Polymarket
prediction markets. It never places real orders and holds no private keys.

Status is tracked in PROGRESS.md. The build plan is in docs/BUILD_SPEC.md.

## Architecture

```mermaid
flowchart LR
    subgraph data[pmsim.data]
        G[Gamma client] --> C[(Catalog)]
        K[CLOB client] --> R[Recorder]
        W[WebSocket feed] --> R
        R --> P[(Parquet / DuckDB)]
    end
    P --> B[pmsim.book<br/>order book + replay]
    B --> E[pmsim.engine<br/>matching, fills, fees]
    E --> S[pmsim.strategies<br/>on_book / on_trade / on_timer]
    S -->|order intents| E
    E --> RK[pmsim.risk<br/>portfolio, resolution]
    RK --> A[pmsim.analysis<br/>metrics, HTML report]
    W -.paper mode.-> B
```

## Quick start

```
uv sync
make check
uv run pmsim --version
```

Configuration lives in `config/default.yaml` and is loaded with
`pmsim.config.load_config`.
