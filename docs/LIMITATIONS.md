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

## Live data freshness (dashboard)

- Prices, Buy/Sell prices and order books stream from Polymarket's public market
  WebSocket, the feed polymarket.com uses. The page shows them within about
  1 to 5 seconds (book every 1 s, market page every 2 s, market grid every 5 s).
- The displayed chance follows Polymarket's rule: the midpoint, or the last trade
  when the spread is wider than 10¢. The last trade comes from Gamma, which
  refreshes about once a minute, so the wide-spread case can lag.
- Only outcomes on screen are streamed: the first two per market card and up to
  60 per market page. Others show Gamma's snapshot, about a minute old.
- Event lists, titles, volumes and new markets come from Gamma, cached 30 s.
- If the WebSocket drops or goes quiet for 30 s, prices fall back to Gamma and
  books to the CLOB REST endpoint until it reconnects.
- The chart history comes from the CLOB `prices-history` buckets with the live
  price appended as the last point.

## Paper trading desk (dashboard)

- Market orders walk the live book once, at the moment you click. A real
  order could see the book move between your click and the match.
- Resting limit orders fill when a later live book crosses their price,
  checked every 5 seconds, at the limit price. Real limit orders can fill
  earlier (a taker hits you between checks) or not at all (queue ahead).
- Bot backtests use the market's real price history but a modeled book (one
  tick spread, deep size), so they overstate fills on thin markets.
- Bots and limit orders only run while `pmsim ui` is running.
- Resolution uses Gamma's final `outcomePrices` once `umaResolutionStatus`
  is `resolved`, checked every minute.
