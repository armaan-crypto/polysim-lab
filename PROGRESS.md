STATUS: IN_PROGRESS
LOCK: 2026-10-27T08:07Z

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
- [ ] Phase 8: Hardening (coverage, profiling, ASSUMPTIONS.md, LIMITATIONS.md)

## Current work (resume here if a session stopped mid-phase)

Phase 8 plan: coverage report (`pytest --cov`) and tests until engine, book and portfolio
modules are above 85%; profile a one-day replay (synthetic 24 h, plus the recorded sample) and
speed up hot spots until it runs under 60 s; write docs/ASSUMPTIONS.md (fee model date,
latency, queue model limits, resolution-risk assumptions) and docs/LIMITATIONS.md (what the
simulator cannot predict). Then the final review at the end of this file. Check `git log`.

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
- The three arbitrage passes come from mispricings the generator injects on purpose. Real books
  may never show them; Polymarket's YES and NO books are linked by the matching engine.
- Latency x3 changes nothing because synthetic books update every 5 s. The latency stress is
  only meaningful on recorded data.
- Cross-platform realized PnL is mostly resolution luck: with 4 markets, one 2% Kalshi
  disagreement swings every pair on that market to $0 or $2. A capture above 1 is not skill.
- Near-resolution buys 0.95-0.99 outcomes whose synthetic win rate is the walk's own probability,
  so it pays the spread and fees for no edge. Market making loses to adverse selection on a
  random walk and pays no fees as a maker.

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
- Paper trading ran live for about 50 s (market maker, 16 Fed tokens): 11 quotes, no fills, log
  and state written, restart resumed. A multi-hour paper run is still to do.
- Kalshi taker fee schedule: assumed 0.07 * C * p * (1 - p). Check the Kalshi fee schedule.
- Kalshi public endpoints reached from this machine; fixtures recorded. The client is untested against
  pagination and rate limits.

## Session log

(newest first; one line per session: UTC time, phase, result)

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
