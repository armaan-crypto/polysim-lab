# Assumptions

Every number the simulator does not observe directly is listed here, with
where it came from. Design reasons are in `DECISIONS.md`.

## Units and rounding

Prices are integers in units of $10^{-4}$, sizes in $10^{-6}$ shares, and cash
in $10^{-10}$ USDC (price times size). Fills and the accounting identity

$$
\text{cash} + \sum_i \text{cost}_i = C_0 + \text{realized PnL}
$$

hold exactly. Fees are rounded to whole micro-USDC per fill; the docs give no
rounding rule.

## Fee model (docs read 2026-10-27)

Taker fee per fill, makers pay nothing (docs.polymarket.com/trading/fees):

$$
f = C \cdot r \cdot p\,(1 - p)
$$

with $C$ shares at price $p$ and $r$ the market's `feeSchedule.rate` from Gamma.
All 500 live markets checked had exponent 1. Markets with no schedule
(`feeType` null) use $r = 0.05$, the "Other / General" rate, as a
conservative default. Gas or settlement cost is a flat amount per fill
(`gas_per_trade_usd`, default 0). Polymarket's taker and maker rebate
programs are not modeled, which understates net returns for market makers.

Kalshi fees use the same form with $r = 0.07$. This is an assumption: the
Kalshi pages found document fee rounding, not the schedule.

## Latency

Orders and cancels reach the simulated exchange `latency_ms` after the
strategy sends them (default 150 ms). Data becomes known to the strategy at
$\max(t_{\text{exchange}}, t_{\text{receive}})$. A 20 s live sample showed
receive minus exchange time between $-5$ ms and $+2.2$ s (snapshots carry the
time of the last book change, not the send time).

## Queue model and fills

- Marketable orders walk the visible book at each level's price. Depth we
  take stays consumed until the recorded level drops by that much or a new
  snapshot arrives.
- A resting order fills only from recorded trade prints at or through its
  price. Its queue position starts at the visible size at that price when it
  arrives and only shrinks: by traded volume, or when the level itself
  shrinks below it. Cancels ahead of us are assumed to come from behind us,
  which is pessimistic.
- A recorded book that moves through our resting price without a print does
  not fill us. The real exchange would match us; the model will not.
- The WebSocket trade `side` field is not used, because the docs do not say
  whose side it is.
- Orders that would cross our own resting orders are rejected.

## Resolution

Winning tokens redeem for $1, losers for $0. Binary markets can resolve
50/50 ($0.50 each); neg-risk groups never split (docs). Timing follows the
UMA flow in the docs: a 2 h challenge period, and 4 to 6 days in total when
disputed (118 extra hours in the model).

The resolution-risk probabilities in `config/default.yaml` are assumptions,
not measurements:

| Class | late proposal | mean delay | dispute | reversal given dispute | 50/50 |
| - | - | - | - | - | - |
| default | 0.10 | 24 h | 0.02 | 0.25 | 0.002 |
| sports | 0.05 | 6 h | 0.01 | 0.25 | 0.002 |
| crypto | 0.02 | 2 h | 0.005 | 0.25 | 0.002 |
| politics | 0.15 | 48 h | 0.05 | 0.25 | 0.002 |

Void outcomes refund cost basis. Polymarket documents no void; the default
probability is 0.

## Neg-risk conversion

One NO share in a market converts into one YES share in every other market
of the group (docs). The NO cost basis is split evenly over the new YES
positions. Merges pay $1 per YES and NO pair.

## Synthetic data

Synthetic markets follow a random walk in log-odds with volatility
$\sigma = 0.3$ per $\sqrt{\text{hour}}$. Books update every 5 s, so the latency stress
has no effect on synthetic runs. Mispricings, flash gaps, stale books,
halts, thin depth and Kalshi disagreement (2% per market) are injected at
configured rates. Every synthetic result is labeled `synthetic`.
