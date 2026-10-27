# polysim-lab: working protocol

This repo holds a Polymarket simulator and strategy backtester. Scheduled
Claude sessions build it in phases. Each session starts with no memory, so
all state lives in this repo. Follow this protocol exactly.

## Every session

1. Read PROGRESS.md, DECISIONS.md and docs/BUILD_SPEC.md.
2. Check the STATUS line at the top of PROGRESS.md:
   - `STATUS: DONE`: print "build complete" and stop. Do nothing else.
   - `LOCK:` set to a time less than 100 minutes ago: another run is active.
     Stop at once.
3. Set `LOCK: <current UTC time>` in PROGRESS.md, commit and push.
4. Find the first unchecked item in PROGRESS.md. Work on that phase only.
   Read its section in docs/BUILD_SPEC.md first.
5. Write tests with the code. Run `make check` (ruff, mypy, pytest).
6. Commit after each working step. Push to `main` after each commit.
7. Update PROGRESS.md: check off finished items, log failures, name the
   next step. Log design choices in DECISIONS.md with a one line reason.
8. When the phase passes `make check`, clear the LOCK, push, and stop.
   Start the next phase in the same session only if less than 25 minutes
   have passed and the context is still small.
9. If every phase is checked, run the final review (see end of PROGRESS.md),
   set `STATUS: DONE`, push, and stop.
10. Never leave the LOCK set when you stop. If a step fails twice, log the
    blocker in PROGRESS.md, clear the LOCK, push, and stop.

## Environment limits

- The cloud sandbox may block Polymarket and Kalshi hosts. Test early with a
  short `curl`. If blocked, do not retry. Build the clients from the official
  docs and recorded fixtures, test with mocks and synthetic data, and add
  the item to the "Needs live verification" list in PROGRESS.md.
- Do not invent API fields. Check docs or fixtures.
- Install with `uv`. If `uv` is missing, `pip install uv --break-system-packages`.

## Safety rules

- The code must never place, sign or submit a real order. No private keys.
- Do not claim a strategy is profitable unless it passes the stress tests on
  recorded or clearly labeled synthetic data. Label synthetic results.
- Keep functions small and typed. No dead code. No secrets in the repo.
- Use LaTeX for math in docs.
- Use thinking effort that fits the phase: deep for Phase 2 (matching engine)
  and Phase 4 (strategies), standard elsewhere.
