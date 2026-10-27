STATUS: IN_PROGRESS
LOCK: none

# Progress

Details for each phase are in docs/BUILD_SPEC.md.

## Phases

- [ ] Phase 0: Scaffold (layout, pyproject, Makefile, config, README with Mermaid)
- [ ] Phase 1: Data layer (Gamma and CLOB clients, recorder, catalog, fixtures, CLI)
- [ ] Phase 2: Order book and matching engine (book, replay, fills, fees, property tests)
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

(none yet)

## Session log

(newest first; one line per session: UTC time, phase, result)
