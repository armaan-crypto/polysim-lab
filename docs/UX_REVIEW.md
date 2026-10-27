# UX review: `pmsim ui` dashboard

Reviewer persona: **Dana**, an experienced Polymarket trader who writes Python bots
and quant algorithms. Reviewed 2026-10-27 against live Polymarket data, `main` at
`61f39cc`. Screenshots are in `docs/ux_review/`. Documented trade-offs in
`docs/LIMITATIONS.md` and `DECISIONS.md` are not repeated unless they hurt usability.

## Overall impression

The ticket, order book and portfolio feel familiar to a Polymarket user within a
minute. Fills walk the real book, cash and realized P&L reconcile to the cent, and
input validation catches every bad amount I tried. The Strategy lab is the most
useful part for a quant: one click gives a verdict, an equity curve, a stress test
and every fill, and dropping a `.py` file into `algorithms/` just works.

Dana stops trusting it in three places:

- **The background desk can die without any sign.** When the desk thread crashed,
  bots still showed "Running" and resting orders stopped filling.
- **Bot backtests are too optimistic.** They charge zero fees and treat "max
  position" as market value, not cost. A dip rule with a stop loss can churn: it
  buys and is stopped out again on every cooldown.
- **Some numbers look wrong at a glance.** The YES book shows the NO side's last
  price. The Yes button price is neither the bid nor the ask. The 3x latency stress
  never changes a result. Sharpe ratios above 200 are shown with no caveat.

Fix those and Dana would use this every day for paper-testing rules before writing
real bot code.

## Bugs

### 1. Desk thread dies on one file error; bots keep saying "Running" (high)

- **Steps:** Start `pmsim ui` twice on the same data dir (this happened here: the
  preview config fell back to port 8766 while another instance held 8765). Create
  and start a bot on one instance.
- **Expected:** The bot trades, or the UI shows an error.
- **Actual:** The `pmsim-desk` thread raised
  `FileNotFoundError: data/desk/bots.tmp -> data/desk/bots.json` in
  `BotManager.save()` and exited. `run_forever` has no exception guard, so bots,
  limit-order matching and resolution all stopped for the rest of the session. The
  bot card still said **Running**, `last_run_ms` stayed 0, and Portfolio showed no
  error. Any `OSError` in `save()` (disk full, a permissions change) has the same
  effect with a single instance.

### 2. Two dashboards on one data dir silently overwrite each other (high)

- **Steps:** Run two instances (as above). Trade and create a bot on instance B.
- **Expected:** One instance refuses to start (a lock file), or both share state
  safely.
- **Actual:** Each process keeps its own in-memory account and bots and overwrites
  `account.json` and `bots.json`. Instance A deleted B's bot from disk within a
  minute. Instance A's History also listed B's trades (`trades.jsonl` is appended
  and read back), while A's cash and positions did not include them. History
  disagreed with the portfolio.

### 3. Bot backtests charge no fees; live bots do (high)

- **Steps:** On "Will the U.S. invade Iran before 2027?" (no fee schedule, so live
  trades pay the documented 5% default rate), backtest any bot over 1W.
- **Expected:** The backtest uses the same fee model as live paper trading.
- **Actual:** `history_data` builds `FeeModel(0.0)`, so every backtest fill has
  `fee 0.0`. A live $25 buy on the same market paid $0.98. Backtests with many
  round trips are overstated, and nothing in the UI says fees are excluded.

### 4. "Buy the dip" with a stop loss churns: buy, stop out, repeat (high)

- **Steps:** New bot on Iran Yes (about 17.5¢). Set buy at or below 18¢, take
  profit 19¢, stop loss 17¢. Backtest 1W.
- **Expected:** The builder warns that the entry is at or below the stop loss, or
  the bot does not buy below its own stop.
- **Actual:** 70 fills, 35 round trips, 0% won, −$99.54. The fills alternate
  BUY 15¢ / SELL 13¢: `decide` buys whenever ask ≤ entry, then the next check sees
  bid ≤ stop and sells, on every cooldown. The default template can do the same:
  in the "All" backtest of the default dip rule, the price crashed through the stop
  and the bot made 103 fills with a 4% win rate. Validation accepts a stop above the
  entry and a take profit below the entry (both tested via `/api/bots/backtest`).

### 5. "Max position ($)" is not a cap on dollars spent (medium)

- **Steps:** Bot: buy $25 at or below 18¢, no exits, max position $100. Backtest
  "All".
- **Expected:** At most $100 is invested.
- **Actual:** 6 buys (15¢, 14¢ ×3, 9¢, 7¢), so $150 was spent. Exposure is measured
  as `shares × current ask`, so the bot keeps averaging down as the price falls. The
  help text says "It stops buying above this exposure", which a trader reads as
  cost.

### 6. Order book "Last" shows the other outcome's price (medium)

- **Steps:** Open "Will the U.S. invade Iran before 2027?" and look at the Yes book.
- **Expected:** Last ≈ 17–18¢.
- **Actual:** "Last 82¢" on the Yes book; the No book also says 82¢. The same thing
  happens on Fed Decision "25 bps increase / No": mid 94.5¢, "Last 5¢". The
  market-level `last_trade_price` is shown on both tokens. Screenshot:
  `ux_review/02-event-binary.png`.

### 7. "Max" / "50%" sell leaves unsellable dust (medium)

- **Steps:** Hold 82.055471 shares (any market buy gives 6-decimal sizes). On the
  Sell tab click **Max**, then **Sell Yes**.
- **Expected:** The position closes.
- **Actual:** Max fills 82.05 and leaves 0.005471 shares. The position stays in
  Portfolio as "0.01 @ 18.7¢, $0.00 (−$0.00)", and Max now fills "0". The only way
  out is to type `0.005471` by hand. 50% of 133.42 gave 66.7 instead of 66.71
  (`Math.floor(66.71*100)` float error).

### 8. Off-tick limit price error is in dollars; the input is in cents (medium)

- **Steps:** Limit buy, price `15.5` (tick 1¢), or `150`, or `99.5`.
- **Expected:** "Price must be a whole number of cents between 1¢ and 99¢."
- **Actual:** "price must be a multiple of 0.01 between 0 and 1". The bot builder
  has the same mix-up: "entry_price: Input should be less than 1",
  "rule: Value error, stop loss must be below take profit" (raw pydantic text with
  internal field names).

### 9. Compare all fails completely if one custom algorithm raises (medium)

- **Steps:** Add `algorithms/x.py` whose `on_book` raises
  `RuntimeError("boom")`. Strategy lab → Compare all → Compare all algorithms.
- **Expected:** That row shows an error; the others still run.
- **Actual:** The whole compare fails with "RuntimeError: boom from Dana". The
  message does not say which file failed and has no traceback. A single run of that
  algorithm says "your algorithm raised RuntimeError: boom from Dana", also with no
  file or line number.

### 10. 3x latency stress never changes the result on simulated data (medium)

- **Steps:** Run every built-in algorithm with Stress test on, simulated 6 h,
  seed 42.
- **Expected:** "Slower (3x)" differs from Normal, at least for arbitrage legs.
- **Actual:** It matches Normal to the cent in all 8 runs (196.11, 938.02, 4.16,
  −421.70, −2692.47, 13.83, −44.43, −108.82). "Double fees" also matches Normal for
  market making (−421.70), because maker fees are 0. Two of the three stresses can
  be no-ops, yet the panel shows them as passed or failed checks. Screenshot:
  `ux_review/11-lab-market-making.png`.

### 11. Market making on recorded data: 134 orders, 0 fills, "never found an entry" (medium)

- **Steps:** Lab → Market making → Data: Recorded → Run.
- **Expected:** "Placed 134 quotes, none filled: the recording has only 1 trade
  print."
- **Actual:** "No trades over the recorded order books: the algorithm never found
  an entry it liked." The recording has 1 trade row, and resting orders only fill
  from trade prints. The verdict blames the algorithm for a data gap.

### 12. Bot P&L and Portfolio P&L disagree for the same shares (low)

- **Steps:** Let a bot buy, then compare its card with Portfolio.
- **Expected:** One mark.
- **Actual:** The bot card marks at the ask (−$0.39), while Portfolio marks at the
  mid. Bot activity also mixes units in one line: "bought 26.68 @ 18.0¢ because ask
  0.180 is at or above 0.17". Screenshot: `ux_review/06-bot-activity.jpg`.

### 13. Prices on outcome buttons go stale; the big chance number does not (low)

- **Steps:** Leave a market page open.
- **Expected:** Outcome buttons and the outcome table follow the live book.
- **Actual:** Only `#chance-now` and the book refresh. Outcome buttons, the "Buy
  Yes 94.5¢" rows and the multi-outcome chance column keep the Gamma prices from
  page load. The dark-mode screenshot shows "95% chance" above a "No change 94%"
  row. Screenshot: `ux_review/07-dark-event.png`.

### 14. Lab equity chart runs past the simulated window (low)

- **Steps:** Run market making, 6 h.
- **Actual:** The x axis runs to 8.0h with a straight segment after 6h
  (post-resolution samples). Dana wonders what happened in hours 6–8.

### 15. A broken algorithm says "Loaded from…" and can still be run (low)

- **Steps:** Add a file with a syntax error, select it in the lab.
- **Actual:** The list correctly shows "Failed to load: SyntaxError: expected ':'
  (<unknown>, line 6)". The detail card says "Loaded from algorithms/dana_broken.py",
  and Run stays enabled; it only errors after you click it. "<unknown>" should be
  the file name. Screenshot: `ux_review/12-lab-broken-algo.png`.

## Confusing or missing

### 16. The Yes/No button price is neither the bid nor the ask (medium)

- **Steps:** Iran market: book 17¢ bid / 18¢ ask.
- **Expected:** "Buy Yes 18¢" (the ask), as on Polymarket.
- **Actual:** "Yes 17.5¢" (Gamma `outcomePrices`), while the fill and toast say
  18¢. Dana reads that as slippage on a $25 order.

### 17. Average price changes between the toast and the position (medium)

- **Steps:** Market buy $25 Yes.
- **Actual:** Toast: "Bought 133.42 Yes at 18¢ avg." Position: "133.42 @ 18.7¢".
  The position average includes the fee; the toast does not. Neither says so.

### 18. Fees are invisible until you type an amount, and missing from History (medium)

- **Steps:** Buy, sell, then open Portfolio → History.
- **Actual:** The ticket never shows the market's fee rate. Fee-free markets and the
  5% default look the same until the preview appears. History "Total" excludes the
  fee ($24.02 for a $25 buy; $11.34 for a sell that paid out $10.87), and there is
  no fee column. History cannot be reconciled with cash.

### 19. Market list is the top ~200 events only; search misses common terms (medium)

- **Steps:** Search "btc"; search for any market outside the top 200 by volume.
- **Actual:** "btc" returns nothing (only titles and questions are matched). The
  page says "199 open events, live from Polymarket", which reads as all of
  Polymarket. Search cannot reach anything outside the fetched set.

### 20. Placeholder and expired markets look tradable (medium)

- **Steps:** Browse Trending.
- **Actual:** 151 placeholder outcomes ("Team I", "Person A", "Party B", "Other")
  show 50% and sort near the top of their cards ("Worlds 2026: Winner: Team I 50%,
  Other 50%"). The bot builder defaults to "Team I: Yes 50¢". Five events whose end
  date has already passed (e.g. "Bulgaria Presidential Election", Oct 25) are still
  listed as open.

### 21. A single-market card hides which outcome it is (low)

- **Steps:** Look at the "Ballon d'Or Winner 2026" card.
- **Actual:** "<1% chance, Yes <0.1¢ / No 100¢" with $26M volume. The one listed
  market is "Will Gavi win…?", but the card never says Gavi, so it reads as "the
  Ballon d'Or will not be awarded". Screenshot: `ux_review/01-markets.png`.

### 22. Backtest resolution and bankroll are not shown (medium)

- **Steps:** Bot backtest on "All".
- **Actual:** "All" uses 12-hourly prices (fills at 07:00 and 19:00 only); 1W is
  roughly hourly. The bot runs every 10 s with a 60 s cooldown, but the test sees
  2 prices a day. The summary's "+3.00%" is against an invisible $1,000 bankroll
  (`max(1000, 2 × max position)`). 1M showed less profit than 1W over a window that
  contains that week, because the 1M data is coarser and misses the 21.5¢ spike.
  The modeled spread is 2 ticks (`bid = mid − t`, `ask = mid + t`), while
  LIMITATIONS.md says "one tick spread".

### 23. Lab metrics that mean little are shown with no caveat (medium)

- **Actual:** Sharpe 22–224 on 6-hour runs. Arbitrage strategies always show
  "Win rate 50%" (one leg wins, one loses). "Realized 310% / 568% / −1288% of its
  expected edge". Market making always shows "Expected $0.00". Dana's lookahead and
  overfitting alarms go off. DECISIONS.md documents the Sharpe noise; the UI should
  too, or hide these numbers when they do not apply.

### 24. Order-ticket previews do not warn before a trade will fail (low)

- **Steps:** Buy $20,000 with $9,980 cash; sell 500 shares while holding 133.
- **Actual:** A full preview appears (avg 21.3¢, fee $753, "You receive $81.47").
  The error only comes after clicking.

### 25. Builder accepts rules that can never trade (low)

- **Steps:** Max position $10, spend per buy $25.
- **Actual:** Saved and backtested with 0 fills and the message "never found an
  entry it liked". The real reason is that the cap is below one order.

### 26. No order history; cancelled limits disappear (low)

- **Actual:** A cancelled limit order leaves no trace. History lists fills only.

### 27. State is lost on refresh (low)

- **Steps:** Run three lab algorithms, or fill in a bot rule, then press Refresh.
- **Actual:** Lab results and "Earlier runs" are gone. The builder keeps the market
  (it is in the URL) but resets every rule. `#lab?algo=x&run=1` re-runs on every
  reload. Back and forward work well otherwise.

### 28. No feedback while a run is queued (low)

- **Actual:** Runs and bot backtests share one lock. During a Compare all, a bot
  backtest just says "Testing" with no queue notice. In the bot builder, clicking a
  search result takes about 3 s with no loading state, and the list stays clickable.

### 29. Phone: the order ticket is about two screens down (low)

- **Steps:** 375 px, open a market.
- **Actual:** The ticket starts at y ≈ 1450 px, below the chart and book. There is
  no sticky Buy/Sell bar, and Portfolio value is hidden in the header (cash only).
  Nothing overflows horizontally. Screenshot: `ux_review/09-phone-event.jpg`.

### 30. Keyboard gaps (low)

- Multi-outcome rows (`.outcome-line`) are `div`s with click handlers, so you
  cannot tab to them. Only their Buy buttons are reachable.
- Buttons, chips and cards have no visible `:focus-visible` style; only inputs and
  search have focus rings.

## Design or copy

### 31. Mixed units and casing (low)

¢ in the ticket, dollars in errors and bot logs. The creation log of an older bot
says "below 91c". Error toasts start lowercase ("minimum order is $1", "not enough
cash"). History "Type" shows raw `market` / `limit`. Some chips are badly cased:
"Us presidential election", "Counter strike 2".

### 32. Rule summaries are imprecise (low)

The card summary says "price is below 17¢". The rule is "at or below" and it
compares the **ask** for entries and the **bid** for exits. Say which side, as the
activity log does.

### 33. Inconsistent volume labels (low)

Cards say "$6.6M Vol. 24h", the market page says "$77.2M Vol." (total), and the
bot builder list shows the 24h figure as "$775K Vol.".

### 34. Chart axes (low)

Tick labels land on odd values (13%, 16%, 20%, 23%; $9,984.15) and dates skip
unevenly (Oct 20, 22, 23, 25, 27). Bot backtest charts use date-only labels even
for the 1D window. Multi-outcome charts show one line, where Polymarket overlays the
top outcomes.

### 35. Small layout nits (low)

- "Buy No 100¢" for a 99.95¢ price.
- Book share column mixes decimals ("108,251.9" next to "53,068.53").
- Bot cards stretch to equal height, leaving large gaps.
- The "Wait between trades" input is misaligned with "Max position" because only
  one of them has help text. Screenshot: `ux_review/04-bot-builder.png`.

### 36. Compare all shows bars only (low)

The API returns fills, Sharpe, drawdown and hit rate, but the UI shows only two
bars and the actual P&L. Expected profit has no number. The seed is used but cannot
be seen or set on this tab. Failed custom algorithms are skipped without notice.
Screenshot: `ux_review/13-lab-compare.png`.

## What worked well

- Market buys walk real depth; partial-cross limit orders take liquidity first and
  rest the remainder, with a clear toast.
- Cash, realized P&L and portfolio value reconciled to the cent after six trades.
- Every bad input was rejected: $0, $0.50, `abc`, `-5`, `1e9`, more than cash,
  selling more than held, below minimum size, off tick.
- Deep links (`#event/...?token=...&side=SELL`) and the Portfolio → Sell flow work.
- Dark and light themes are both clean. No horizontal page scroll at 375 px.
- Custom algorithms load on reload, show their settings form, and syntax errors
  appear inline with a line number.

## Dana's top 5 fixes

1. **Make the desk unkillable and visible.** Catch exceptions per step in
   `run_forever`, show desk health (last tick, last error) in the header and on bot
   cards, and refuse to start a second instance on the same data dir. Use a lock
   file and unique temp names. (Findings 1, 2.)
2. **Make bot backtests honest.** Use the market's real fee model, show the bar
   resolution and bankroll, cap max position by cost, and block or warn on entry ≤
   stop loss and take profit ≤ entry. (Findings 3, 4, 5, 22, 25.)
3. **Fix the numbers Dana sees first.** Use the per-token last trade price, show the
   best ask on Buy buttons and the best bid on Sell, refresh outcome prices with the
   book, and make one average price (with or without fees, labeled) everywhere.
   (Findings 6, 13, 16, 17.)
4. **Make positions closable and fees traceable.** Max and % buttons should sell
   the exact held size. Add fee and net-cash columns to History. Show the fee rate in
   the ticket. (Findings 7, 18.)
5. **Make stress tests and lab metrics mean something.** Make latency actually
   matter on synthetic data (or drop that stress), skip stresses that cannot change
   a strategy, explain recorded-data fill limits, caveat or hide Sharpe, win rate
   and edge capture where they do not apply, and isolate custom-algorithm failures
   in Compare all. (Findings 9, 10, 11, 23.)
