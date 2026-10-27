STATUS: IN_PROGRESS
LOCK:

# Progress

Details for each phase are in docs/BUILD_SPEC.md.

## Phases

- [x] Phase 0: Scaffold (layout, pyproject, Makefile, config, README with Mermaid)
- [x] Phase 1: Data layer (Gamma and CLOB clients, recorder, catalog, fixtures, CLI)
- [x] Phase 2: Order book and matching engine (book, replay, fills, fees, property tests)
- [ ] Phase 3: Portfolio, resolution and accounting (PnL, resolution risk, neg-risk)
- [ ] Phase 4: Strategy framework (interface, lookahead guard, six reference strategies)
- [ ] Phase 5: Backtester and experiment runner (event-driven, sweeps, synthetic data, stress)
- [ ] Phase 6: Metrics and reports (metrics, theory vs realized edge, HTML report)
- [ ] Phase 7: Paper trading mode (live feed, no-order guard test)
- [ ] Phase 8: Hardening (coverage, profiling, ASSUMPTIONS.md, LIMITATIONS.md)

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

## Session log

(newest first; one line per session: UTC time, phase, result)

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
