STATUS: IN_PROGRESS
LOCK: 2026-10-27T07:49Z

# Progress

Details for each phase are in docs/BUILD_SPEC.md.

## Phases

- [x] Phase 0: Scaffold (layout, pyproject, Makefile, config, README with Mermaid)
- [x] Phase 1: Data layer (Gamma and CLOB clients, recorder, catalog, fixtures, CLI)
- [x] Phase 2: Order book and matching engine (book, replay, fills, fees, property tests)
- [x] Phase 3: Portfolio, resolution and accounting (PnL, resolution risk, neg-risk)
- [x] Phase 4: Strategy framework (interface, lookahead guard, six reference strategies)
- [x] Phase 5: Backtester and experiment runner (event-driven, sweeps, synthetic data, stress)
- [ ] Phase 6: Metrics and reports (metrics, theory vs realized edge, HTML report)
- [ ] Phase 7: Paper trading mode (live feed, no-order guard test)
- [ ] Phase 8: Hardening (coverage, profiling, ASSUMPTIONS.md, LIMITATIONS.md)

## Current work (resume here if a session stopped mid-phase)

Phase 6 plan: `analysis/metrics.py` (net PnL, return on capital, annualized return on locked
capital, Sharpe from the equity curve, max drawdown, hit rate, average edge per trade, fill rate,
leg-failure rate, slippage vs quoted edge, resolution-loss count), theory vs realized edge per
strategy, `analysis/report.py` (static HTML per run: equity curve PNG via matplotlib, trade table,
failure log, synthetic label banner), `pmsim report` CLI. Check `git log` for what landed.

## Synthetic stress results (SYNTHETIC DATA, default SyntheticConfig, 6 h, seed 42)

These are labeled synthetic. They show the machinery works; they say nothing about live profit.

| Strategy | base | latency x3 | depth x0.5 | fees x2 | verdict |
| - | - | - | - | - | - |
| complementary | $143.00 | $143.00 | $127.01 | $110.13 | pass (synthetic) |
| negrisk_sum | $22.31 | $22.31 | $11.15 | $18.11 | pass (synthetic) |
| near_resolution | -$2,703.70 | -$2,703.70 | -$2,709.37 | -$2,718.62 | fail |
| market_making | -$128.53 | -$128.53 | -$55.85 | -$128.53 | fail |
| cross_platform | $857.74 | $857.74 | $717.70 | -$228.89 | fail |

Notes: the arbitrage passes come from mispricings the generator injects on purpose. Latency x3
changes nothing because synthetic books update every 5 s. Cross-platform PnL is dominated by the
2% per-market chance that Kalshi resolves differently: with 4 markets one disagreement swings
every pair on that market to $0 or $2.

## Final review (after Phase 8)

- [ ] Run `make check` and the full test suite
- [ ] Re-read the safety rules in CLAUDE.md and confirm each holds
- [ ] Write a short "how to run it" section in README.md
- [ ] Set `STATUS: DONE` at the top of this file

## Needs live verification (on a machine that can reach Polymarket)

- Multi-hour `pmsim record` run (only an ~20 s live run so far). Reconnect logic
  is tested with fake sockets only.
- `custom_feature_enabled` events (`best_bid_ask`, `new_market`,
  `market_resolved`) are not subscribed or recorded yet.
- `tick_size_change` frames: parser built from the docs example; none seen live.
- Fill model queue estimates and latency have not been compared with real fills (no real orders, by design).
  Resting orders can only fill from trade prints; the WS trade `side` meaning is unverified.
- Kalshi taker fee schedule: assumed 0.07 * C * p * (1 - p). Check the Kalshi fee schedule.
- Kalshi public endpoints reached from this machine; fixtures recorded. The client is untested against
  pagination and rate limits.

## Session log

(newest first; one line per session: UTC time, phase, result)

- 2026-10-27T07:49Z: Phase 5 done, `make check` passes (178 tests). Synthetic generator, event-driven
  backtester, mock Kalshi venue, registry, sweeps, walk-forward, parallel runs, stress tests,
  `pmsim backtest`. First synthetic runs exposed a half-updated-pair bug, a stale own-impact view
  and a market-maker sizing bug; all fixed. Next: Phase 6 (metrics and reports).
- 2026-10-27T07:35Z: Phase 4 done, `make check` passes (156 tests). Strategy interface, lookahead guard, leg
  manager with unwinds, six reference strategies, Kalshi read-only client and mock feed. Tests
  caught an unwind-proceeds sign bug. Next: Phase 5 (backtester, synthetic data, stress tests).
- 2026-10-27T07:27Z: Phase 3 done, `make check` passes. Portfolio (average cost, PnL, lock-up, idle cash, merge,
  neg-risk conversion), resolution payouts and settlement, seeded resolution-risk simulator with
  per-class profiles (assumed probabilities, documented UMA timings). Next: Phase 4 (strategies).
- 2026-10-27T07:15Z: Phase 2 done, `make check` passes (92 tests, hypothesis properties for L2, L3 and
  the fill engine). Property test found an unknown-id cancel crash, fixed. Live replay of a 20 s
  recording matches the feed's best bid/ask on 160/160 tokens. Next: Phase 3 (portfolio, resolution).
- 2026-10-27T07:10Z: Phase 1 done, `make check` passes (45 tests). Network reachable; fixtures
  recorded from live Gamma, CLOB, data-api and WS. Live smoke: fetch 500 markets, record 176
  tokens for 20 s (18.7k snapshot rows, 4.3k deltas, 1 trade). Took over a stale LOCK from a
  scheduled run that stopped without committing. uv was missing locally; installed via pip
  (binary at ~/Library/Python/3.13/bin). Fixed: live Gamma markets can have null `feeType`
  and missing `feeSchedule`/`resolutionSource`/`endDate`. Next: Phase 2.
- 2026-10-27T06:35Z: Phase 0 done, `make check` passes (7 tests). Next: Phase 1 (data layer). Network access to Polymarket not yet tested.
