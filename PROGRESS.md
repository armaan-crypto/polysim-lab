STATUS: DONE
LOCK:

# Progress

Details for each phase are in docs/BUILD_SPEC.md.

## Phases

- [x] Phase 0: Scaffold (layout, pyproject, Makefile, config, README with Mermaid)
- [x] Phase 1: Data layer (Gamma and CLOB clients, recorder, catalog, fixtures, CLI)
- [x] Phase 2: Order book and matching engine (book, replay, fills, fees, property tests)
- [x] Phase 3: Portfolio, resolution and accounting (PnL, resolution risk, neg-risk)
- [x] Phase 4: Strategy framework (interface, lookahead guard, six reference strategies)
- [x] Phase 5: Backtester and experiment runner (event-driven, sweeps, synthetic data, stress)
- [x] Phase 6: Metrics and reports (metrics, theory vs realized edge, HTML report)
- [x] Phase 7: Paper trading mode (live feed, no-order guard test)
- [x] Phase 8: Hardening (coverage, profiling, ASSUMPTIONS.md, LIMITATIONS.md)

## Current work

None. All phases and the final review are done. Remaining work is the "Needs live
verification" list below.

## Performance (one-day replay, SYNTHETIC DATA, `make bench`)

441,596 events (24 h, 9 markets, 18 tokens) generated in 1.8 s. Backtest time per strategy:
complementary 4.3 s, cross_platform 7.2 s, logical 1.5 s, market_making 7.1 s,
near_resolution 3.2 s, negrisk_sum 4.5 s (60k to 300k events/s). Target was under 60 s.
At the live sample's rate (about 220 deltas/s across 176 tokens), a full recorded day of all
176 tokens is about 19M events, roughly 5 minutes; about 20 tokens fit under 60 s.

## Synthetic stress results (SYNTHETIC DATA, default SyntheticConfig, 6 h, seed 42)

These are labeled synthetic. They show the machinery works; they say nothing about live profit.
Run `uv run pmsim backtest --strategy NAME --stress` to reproduce.

| Strategy | base | latency x3 | depth x0.5 | fees x2 | verdict | edge capture |
| - | - | - | - | - | - | - |
| complementary | $196.11 | $196.11 | $167.34 | $123.53 | pass (synthetic) | 1.00 |
| negrisk_sum | $13.83 | $13.83 | $6.81 | $6.23 | pass (synthetic) | 1.00 |
| logical | $4.16 | $4.16 | $2.08 | $1.66 | pass (synthetic) | 1.00 |
| cross_platform | $938.02 | $938.02 | $208.85 | $71.13 | pass (synthetic) | 3.10 |
| near_resolution | -$2,692.47 | -$2,692.47 | -$2,701.99 | -$2,707.94 | fail | -8.76 |
| market_making | -$421.70 | -$421.70 | -$212.11 | -$421.70 | fail | n/a |

Notes, read before quoting any number above:
- The three arbitrage passes come from mispricings the generator injects on purpose. In the recorded
  live sample all 78 two-sided binary markets had mirrored YES/NO books and none had an ask sum
  below $1, so strategy 1 looks structurally dead on Polymarket.
- Latency x3 changes nothing because synthetic books update every 5 s. The latency stress is
  only meaningful on recorded data.
- Cross-platform realized PnL is mostly resolution luck: with 4 markets, one 2% Kalshi
  disagreement swings every pair on that market to $0 or $2. A capture above 1 is not skill.
- Near-resolution buys 0.95-0.99 outcomes whose synthetic win rate is the walk's own probability,
  so it pays the spread and fees for no edge. Market making loses to adverse selection on a
  random walk and pays no fees as a maker.

## Final review (after Phase 8)

- [x] Run `make check` and the full test suite (202 tests, 97.7% coverage on engine and book)
- [x] Re-read the safety rules in CLAUDE.md and confirm each holds:
  - No real orders, signing or keys: no trading or signing packages, no order endpoints, GET-only
    HTTP client; enforced by tests/test_no_trading.py. Secret scan of src, tests, scripts, config
    and docs found nothing.
  - No profitability claims: every result is labeled synthetic or recorded; the synthetic
    passes are explained as injected mispricings, and recorded data shows no complementary edge.
  - Small typed functions, no dead code: mypy strict passes; an unreferenced-definition scan
    removed `Position.avg_price` and added a test for `KalshiClient`.
  - LaTeX for math in docs: METRICS, ASSUMPTIONS, LIMITATIONS and DECISIONS formulas.
- [x] Write a short "how to run it" section in README.md
- [x] Set `STATUS: DONE` at the top of this file

## Needs live verification (on a machine that can reach Polymarket)

- Multi-hour `pmsim record` run (only an ~20 s live run so far). Reconnect logic
  is tested with fake sockets only.
- `custom_feature_enabled` events (`best_bid_ask`, `new_market`,
  `market_resolved`) are not subscribed or recorded yet.
- `tick_size_change` frames: parser built from the docs example; none seen live.
- Fill model queue estimates and latency have not been compared with real fills (no real orders, by design).
  Resting orders can only fill from trade prints; the WS trade `side` meaning is unverified.
- Paper trading ran live for about 50 s (market maker, 16 Fed tokens): 11 quotes, no fills, log
  and state written, restart resumed. A multi-hour paper run is still to do.
- Kalshi taker fee schedule: assumed 0.07 * C * p * (1 - p). Check the Kalshi fee schedule.
- Kalshi public endpoints reached from this machine; fixtures recorded. The client is untested against
  pagination and rate limits.

## Session log

(newest first; one line per session: UTC time, phase, result)

- 2026-10-27T15:23Z: Dashboard now works like Polymarket with paper money: live markets and books, buy/sell
  with market and limit orders, portfolio with auto settlement, no-code bots with real-history
  backtests and live paper runs, strategy lab with presets and custom algorithms. Added a request
  guard and localhost IPv6. `make check` passes (242 tests). Verified live: $20 buy filled at the
  live ask, bot backtest and start worked.
- 2026-10-27T14:42Z: Added the local dashboard (`pmsim ui`, user request after STATUS DONE): Polymarket-style
  markets view with recorded order books, backtest, compare and paper views; JSON API with tests
  (222 tests, `make check` passes). Catalog now stores prices, volume, icons and event titles.
- 2026-10-27T08:25Z: Phase 8 and final review done, `make check` passes (202 tests, coverage gate 85%,
  actual 97.7%). Profiling found full scans of all orders, quotes and leg groups; indexing and
  binary search made runs about 7x faster and a 24 h synthetic day replays in under 8 s.
  Wrote ASSUMPTIONS.md and LIMITATIONS.md; a recorded-data check showed YES/NO books mirror
  exactly (78/78 markets). STATUS set to DONE.
- 2026-10-27T08:07Z: Phase 7 done, `make check` passes (201 tests). `pmsim paper` drives the backtest loop
  from the live WebSocket feed through the recorder (same fill model), logs every decision with
  a reason to JSONL, saves state atomically and resumes it. Safeguard tests fail on trading or
  signing imports, order endpoints or HTTP write calls. Next: Phase 8 (hardening).
- 2026-10-27T08:02Z: Phase 6 done, `make check` passes (187 tests). Metrics (docs/METRICS.md in LaTeX),
  theory vs realized edge (`pmsim edges`), HTML report (`pmsim backtest --report`), synthetic
  nested markets so the logical strategy runs. Next: Phase 7 (paper trading).
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
