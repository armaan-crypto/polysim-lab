# Metrics

Every run reports the metrics below (`pmsim.analysis.metrics`). Money is in
USD. $C_0$ is starting cash, $E_t$ the equity curve sampled every
`equity_every_ms`, and $\Pi = E_T - C_0$ the net PnL.

| Metric | Definition |
| - | - |
| Net PnL | $\Pi = E_T - C_0$, after fees, gas, unwinds and resolution |
| Return on capital | $\Pi / C_0$ |
| Annualized return on locked capital | $\dfrac{\Pi}{\bar K} \cdot \dfrac{Y}{T}$ with $\bar K = \frac{1}{T}\int_0^T K_t\,dt$, $K_t$ the cost basis held, $Y$ one year |
| Sharpe | $\dfrac{\bar r}{s_r}\sqrt{n_Y}$, $r_i = E_{i}/E_{i-1} - 1$, $n_Y$ samples per year, zero risk-free rate |
| Max drawdown | $\max_t \left(\max_{u \le t} E_u - E_t\right)$, also as a fraction of the peak |
| Hit rate | share of closed positions with PnL $> 0$ |
| Average edge per entry | theoretical edge summed over entries, divided by entries that carried an edge |
| Average PnL per position | realized PnL summed over closed positions, divided by their count |
| Fill rate | orders with at least one fill, divided by orders sent |
| Leg-failure rate | multi-leg groups that ended with unequal legs, divided by groups opened |
| Theoretical edge | sum of the edge each strategy computed when it entered, after fees and gas |
| Slippage vs quoted edge | theoretical edge $-\ \Pi$ |
| Unwind cost | basis of unwound excess legs minus what selling them fetched |
| Resolution losses | positions redeemed below their cost basis at resolution |
| Idle cash fraction | $\int c_t\,dt \big/ \left(\int c_t\,dt + \int K_t\,dt\right)$, $c_t$ cash |

## Theoretical versus realized edge

For a complementary pair bought at the asks $P_{YES}$ and $P_{NO}$, $q$ shares each:

$$
\text{edge} = q\left(1 - P_{YES} - P_{NO}\right) - f(P_{YES}, q) - f(P_{NO}, q) - 2g,
\qquad f(p, q) = q \cdot r \cdot p(1-p)
$$

with $r$ the market's taker fee rate and $g$ gas per fill. The other strategies
use the same pattern with their own payout. Realized edge is the net PnL of the
run. The capture ratio $\Pi / \text{edge}$ is 1 when every leg fills at the
quoted price and every market resolves as the strategy assumed. It falls with
leg failures, unwinds, depth taken by others and resolution surprises.

Synthetic runs are labeled `synthetic` in every summary and report.
