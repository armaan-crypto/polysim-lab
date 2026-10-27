# Limitations

What this simulator cannot tell you. Read this before trusting a backtest.

## It never trades, so the fill model is unvalidated

No real order has ever been placed (by design), so queue position, latency
and partial fills have not been compared with real fills. Resting orders
only fill from trade prints at or through their price; real fills can come
earlier (we were ahead of cancels) or later (hidden size, other takers).

## Our own impact is crude

Depth we take is subtracted until the recorded book shows it gone. The
simulator cannot know how other traders would have reacted to our orders:
a real arbitrage gets competed away, a real quote changes others' quotes.
Recorded data shows the world without us.

## YES and NO books mirror each other on the real exchange

In the recorded live sample (2026-10-27, 78 binary markets with two-sided
books), every YES book was the exact mirror of its NO book: best YES bid
$= 1 -$ best NO ask and best YES ask $= 1 -$ best NO bid, and no pair had
$P_{YES} + P_{NO} < 1$ at the asks. Complementary arbitrage on one market
therefore looks structurally absent on Polymarket. The synthetic generator
injects such mispricings on purpose; those profits say nothing about live
markets.

## Resolution risk is a guess

Dispute, reversal and 50/50 probabilities are assumed per market class (see
`ASSUMPTIONS.md`). Rule-text risk scores are keyword counts and miss most
real ambiguity. Clarifications ("additional context") issued after trading
starts are not modeled.

## Cross-venue results depend on unverified inputs

The Kalshi side uses a mock venue that fills at the quote, an assumed fee
rate, and a hand-set mismatch score. A real Kalshi leg can fail, fill worse,
or resolve differently for reasons no score captures. Kalshi requires its
own account and funding, which the simulator ignores.

## Capital, funding and costs left out

- No deposit or withdrawal delays, bridge costs or currency risk (pUSD/USDC).
- No taker or maker rebates, no liquidity rewards, no holding rewards.
- Gas is a flat per-fill number; real settlement cost varies.
- Cash idle between trades earns nothing.

## Data limits

- Recorded data covers what the recorder saw: no history before recording
  started, gaps during disconnects, and only the subscribed tokens.
- The public trades endpoint has no queue or order IDs, so trades cannot be
  tied to book levels exactly.
- Augmented neg-risk events can add outcomes after trading starts; groups
  are built from the catalog at fetch time and are not updated.

## Statistics

Short runs give noisy Sharpe ratios and drawdowns, and the synthetic
universe has few markets, so one resolution can swing a whole run. Walk-
forward folds are small. Treat any single number as anecdote.

## Paper trading

Paper trading uses the same fill model on live data, so it shares every
limit above. A restart drops open simulated orders and multi-leg state.
