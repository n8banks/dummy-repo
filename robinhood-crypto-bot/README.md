# rhbot: a risk-managed crypto trader for Robinhood

A Python bot for the official **Robinhood Crypto Trading API**. It includes:

- a coin screener,
- a backtester that charges realistic costs,
- a research harness that compares strategies against buy-and-hold,
- a paper trader (the default),
- a live trader behind two explicit safety switches.

> **Read this first.** On real 2022–2026 prices, the first version of this bot lost
> money. That version traded hourly and aimed for "1000 small trades". The version
> here trades daily, sits out bear markets, and made a small profit in testing. But
> it's a thin edge, not a money machine. Only risk money you can lose. Nothing here
> is financial advice.

---

## 1. The spec (your prompt, improved)

> Build a Python trading system for the **official Robinhood Crypto Trading API**
> (Ed25519-signed requests to `trading.robinhood.com`). It trades liquid, mainstream
> spot crypto: BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, LINK, LTC and BCH, against USD.
>
> - **Goal:** grow a small, regularly topped-up pot by taking many small,
>   cost-aware trades. Only take a trade when the expected move clearly beats
>   Robinhood's spread.
> - **Budget:** $25 per day of new capital, capped at $500 to start. The bot
>   compounds its own profits and never spends other account cash or sells coins you
>   bought yourself.
> - **Diversification:** at most 10% of the pot in any one coin, and at most 8
>   positions open at once.
> - **Loss cap:** no trade may lose more than 5% (stop at most 5% below entry).
>   After a 3% loss in a day, stop buying for the day. After a 15% drop from the
>   peak, sell everything and stop until a human restarts it.
> - **Evidence:** backtest on **real** historical data with realistic costs. Compare
>   against buy-and-hold. Check that results hold across different settings and
>   years, not just one lucky combination.
> - **Operations:** paper trading by default. Live trading needs explicit opt-in.
>   Restarts are safe. Stop-loss orders rest at Robinhood so crashes are covered even
>   if the bot is down. Runs 24/7 on a home desktop, with phone alerts.
> - **Deadline:** running by Friday.

---

## 2. Research: the Robinhood Crypto Trading API

| | |
|---|---|
| **Base URL** | `https://trading.robinhood.com` |
| **Auth** | Headers `x-api-key`, `x-timestamp` (unix seconds, valid ~30 s) and `x-signature`. The signature is Ed25519 over `api_key + timestamp + path_with_query + METHOD + body`, using a base64 32-byte seed as the private key. |
| **Rate limit** | 100 requests/minute, bursting to 300 |
| **Endpoints (v1)** | `trading/accounts/`, `trading/trading_pairs/`, `trading/holdings/`, `trading/orders/` (plus `{id}/cancel/`), `marketdata/best_bid_ask/`, `marketdata/estimated_price/` |
| **Order types** | `market` (asset quantity only), `limit`, `stop_loss`, `stop_limit` |
| **Quotes** | Include `bid_inclusive_of_sell_spread` / `ask_inclusive_of_buy_spread`, which are the prices you actually get. The bot measures costs from these. |
| **Not available** | Price history, shorting, leverage, derivatives, websockets |

Robinhood's own docs site was unreachable from the build environment. The details
above come from an open-source client that implements Robinhood's published spec
([nirholas/robinhood-mcp](https://github.com/nirholas/robinhood-mcp)). **The live
order code has never been run against the real API.** Your first `trade --once`
with keys is its first real test.

**Costs are the whole game.** There's no commission, but a spread is built into
your price: roughly **0.35–0.85% for BTC/ETH**, wider for smaller coins and during
volatile moves. So a buy plus a sell costs about **0.7–1.7%**.
([Robinhood fee tiers](https://robinhood.com/us/en/support/articles/crypto-fee-tiers),
[Bitget: RH spreads 2026](https://www.bitget.com/academy/robinhood-crypto-trading-spreads-explained-2026-america-beginners-guide-costs-features-new-tools))

---

## 3. What real data says

Backtests used real hourly candles from **Jan 2022 to Sep 2026** for BTC, ETH, SOL,
XRP, DOGE, ADA and BCH. The data is Binance's, via the public-domain
[Speirsy11/crypto-dataset](https://github.com/Speirsy11/crypto-dataset). Every test
charges a 0.8% round-trip spread, starts with $10k, and has the kill switch turned
off so the full period is visible.

| Variant | CAGR | Sharpe | Worst drop | 2022 | 2023 | 2024 | 2025 |
|---|---|---|---|---|---|---|---|
| hourly trend (v1) | −12% before halt | −2.7 | tripped kill switch in 2022 | | | | |
| 4-hour momentum | −10.6% | −0.45 | 48% | −18% | −3% | −3% | −24% |
| daily trend + regime | 2.7% | 0.31 | 14% | −1% | +6% | +21% | −7% |
| **daily momentum + regime (default)** | **4.7%** | **0.33** | **34%** | **−4%** | **+16%** | **+31%** | **−16%** |
| same, with a 10% loss cap | 4.4% | 0.43 | 20% | −3% | +16% | +18% | −8% |
| buy & hold, equal weight | −1.7% | 0.30 | 73% | −73% | +125% | +126% | −21% |
| buy & hold, BTC only | 12.8% | 0.49 | 67% | −65% | +156% | +121% | −6% |

(CAGR = average yearly growth. Sharpe = return per unit of risk; higher is better.
"Regime" = only buy while BTC is above its 100-day average.)

**Robustness checks:**
- 68 of 72 settings for daily momentum + regime made money (typical: +4.9%/yr).
- All 27 settings for daily trend + regime made money (+2–4%/yr).
- The default is a middle-of-the-range setting, not the best backtest. Picking the
  best one mostly picks luck.

**Spread sensitivity** (daily momentum + regime):

| Round-trip spread | Yearly return |
|---|---|
| 0.2% | +11.5% |
| 0.4% | +9.0% |
| 0.8% | +4.7% |
| 1.2% | +0.5% |
| 1.6% | −3.5% |

**$25/day since 2022:** $27.6k deposited grew to $37.5k (+11.8%/yr on the money
deposited) before the 15% kill switch tripped in 2025.

**$500 lump sum:** results swing a lot depending on timing:
- 5% loss cap: tripped the kill switch in 2023 at −$55.
- 5% cap plus 4% interest on idle cash: +8.9%/yr until a halt in 2024.
- 10% loss cap plus interest: +8.2%/yr, a 14% worst drop, and no halt.

---

## 4. Honest review of this project

These are the problems that matter, not every nitpick.

1. **The original idea fails on Robinhood's costs.** Frequent small trades pay
   the spread every time. On real data the hourly version lost money at both 0.4%
   and 0.8% spreads, and the 4-hour version lost 7–11% a year. "1000 small
   trades" only works where trading costs about 0.1%, not 0.8%. The bot now trades
   daily: about 90 trades a year across 7 coins.
2. **The edge that survives is thin.** It's about +5% a year at a 0.8% spread, and
   it disappears around 1.2%. It **did not beat simply holding BTC** on either return
   or risk-adjusted return. What it does well is avoid crashes: it lost 4% in 2022
   while the coins fell 65–73%. If the spreads you actually pay are near 1%, there's
   no edge.
3. **The backtest flatters itself in known ways:**
   - it's one 4¾-year history,
   - the prices are Binance's, not Robinhood's,
   - the coins were chosen in 2026, so none of them blew up (no LUNA or FTT),
   - it assumes a fixed spread, but real spreads widen in crashes, exactly when
     stops fire,
   - it covers 7 of the 10 coins.

   Expect live results to be worse than the backtest.
4. **Your 5% loss rule has a measurable cost.** Crypto routinely moves 5% in a day,
   so tight stops sell at the bottom of normal swings and buy back higher. In every
   test, a 10% cap had better risk-adjusted returns and smaller worst drops. The
   15% kill switch also trips in a normal bad year, which means pausing and
   reviewing, not a disaster. Both are your call (`--max-loss`, see section 7).
5. **The live code is unproven.** Order handling follows a third-party client of
   Robinhood's spec, and I couldn't test it against the real API. Paper mode and 25
   unit tests cover the logic, not the real endpoint.
6. **The screener is heuristic.** Its weights are judgment calls, and its "out of
   sample" check is too short to mean much. Use it to spot bad coins, like ones
   whose spread is too wide for their typical moves. Don't use it to pick winners.

**What's solid:**
- the signed API client,
- honest cost accounting: next-bar fills, gap-through stops, deposits not counted
  as profit,
- the budget ledger, which never spends outside money or sells your own coins,
- stops that rest at Robinhood,
- crash-safe state files,
- the research harness that exposed all of the above.

---

## 5. How this compares to other crypto strategies

| Approach | Used by | Workable here? |
|---|---|---|
| Market making / high-frequency trading | Firms with exchange connectivity and fee rebates | **No.** Robinhood routes orders to market makers, so you can't post quotes. |
| Funding-rate / basis arbitrage (buy spot, short futures, earn the funding payments) | Hedge funds, advanced retail on derivatives exchanges | **No.** This API has no futures or perpetuals. It's the most consistent crypto yield, but it needs a derivatives venue. |
| Cross-sectional momentum (rank many coins, hold the strongest) | Quant funds; documented in academic research | **Partly.** It needs 30+ coins to work; 7–10 is too few. |
| **Trend / time-series momentum, sized by volatility** | CTAs and trend funds; the strongest-documented crypto effect (e.g. Liu & Tsyvinski, *Risks and Returns of Cryptocurrency*, 2021) | **Yes. This is what rhbot now does**, long-only, with a BTC regime filter. |
| Grid bots | Popular retail bots | **Poorly.** They profit in ranges and lose in trends, and Robinhood's spread forces grid lines far apart. |
| "AI"/sentiment bots | Marketing | Mostly overfitted to past data. No edge anyone has shown after costs. |
| Regular buying (DCA) plus a trend filter | Passive investors | **Yes, and simpler.** It's close to what the budget ledger plus regime filter already does. |

So rhbot uses the right tool for Robinhood's constraints. **Robinhood is simply an
expensive venue to trade actively on.** The same strategy at a 0.2–0.4% spread made
+9–11% a year. If this experiment proves out, running it on a lower-cost exchange is
the biggest single improvement available. That would take a new broker module.

---

## 6. Is this an income stream?

Not at this size, and it's worth being blunt about that:

- **$500 at about 5% a year is about $25 a year.** Even a great year (+30%) is $150.
- **Most of the growth comes from your deposits.** $25/day is about $9,000 a year.
  The trading adds a few percent on top. The deposit habit is the real wealth
  builder.
- **Idle cash earns interest.** The bot is in cash about half the time. With
  Robinhood Gold, uninvested cash earns the sweep rate (check the current rate in
  the app). That's real, low-risk return, and at 4% it doubled the $500 backtest's
  yearly return. Use `backtest --cash-apy 0.04` to see it with your rate.
- **Treat the first 3–6 months as an experiment, not income.** The goal is to find
  out whether the edge survives Robinhood's real spreads. If it does, scale up
  slowly. If it doesn't, you've lost a capped, known amount.

---

## 7. How it works

```
rhbot/
  robinhood.py   signed API client, rate limiter, order-body builder
  data.py        Coinbase candles, CSV cache, resampling, synthetic data
  indicators.py  EMA/SMA/ATR/RSI/Donchian/volatility/correlation
  strategies.py  TSMomentum (default), TrendBreakout, MeanReversion, RegimeFilter
  risk.py        sizing, 5% loss cap, exposure caps, circuit breakers
  budget.py      $25/day allowance with a cap
  backtest.py    portfolio backtester (spread, fees, gaps, deposits, cash interest)
  research.py    variant comparison vs buy & hold, per-year returns
  screener.py    coin ranking
  broker.py      PaperBroker / RobinhoodBroker (limit entries, native stop-loss orders)
  trader.py      daily decision loop plus a stop check every 15 minutes, phone alerts
  util.py        crash-safe JSON writes, ntfy notifications
```

**Strategy (default `tsmom` with the regime filter, on daily bars):** each coin gets a
vote from 5 lookbacks: is the price higher than it was 7, 14, 30, 60 and 90 days
ago? The bot buys when at least 4 of 5 say "up" **and** BTC is above its 100-day
average. It sells when 2 or fewer say "up", or when the trailing stop is hit.

**Risk defaults:**
- 0.5% of the pot at risk per trade.
- Stop at 2.5×ATR or 5% below entry, whichever is closer. Trailing stop at 3×ATR.
- At most 10% of the pot per coin, 70% invested in total, 8 positions.
- Stop buying for the day after a 3% daily loss. After a 15% drop from the peak,
  sell everything and halt.

**Change these to your preference:**
- `--max-loss 0.10` loosens the per-trade cap (see section 4).
- `--no-regime` turns off the BTC filter.
- `--strategy trend` is the calmer, lower-return option.

---

## 8. Usage

```bash
pip install -r requirements.txt

python -m rhbot fetch                        # ~5 years of daily candles from Coinbase
python -m rhbot research                     # strategy comparison table, like section 3
python -m rhbot backtest --daily-budget 25 --cash-apy 0.04
python -m rhbot backtest --equity 500 --spread 0.012          # stress test: wider spread
python -m rhbot screen                       # uses live Robinhood spreads once keys are set

python -m rhbot keygen                       # key pair for Robinhood API access
python -m rhbot trade --once                 # one paper cycle: prints what it would do
python -m rhbot trade                        # paper trade continuously
python -m rhbot status                       # pot, positions, stops, realized P&L
```

Trading defaults: `--interval 1d --strategy tsmom --daily-budget 25 --budget-cap 500`.

**Going live** needs both of these:

```bash
export RHBOT_LIVE_ACK="I understand this trades real money"
python -m rhbot trade --live
```

**API keys:** run `keygen`. Register the **public** key in Robinhood under
**Account → Crypto → API Trading**, and put the API key it gives you plus your
**private** key in the env file on your desktop. Never paste them into a chat, a
commit or a screenshot.

---

## 9. Plan for Friday

1. **Wed:** set up the desktop (section 10). Run `fetch`, `research` and
   `backtest --daily-budget 25` there. These use Coinbase data up to today, which
   is a fresh check on this document's numbers.
2. **Wed:** create your keys and start **paper** trading
   (`systemctl enable --now rhbot`). Check that `trade --once` and `status` make
   sense, and that the spreads `screen` reports are near 0.8%. If they're above
   about 1.1%, there's no edge. Don't go live.
3. **Fri:** fund with $500, add `--live`, and keep `--daily-budget 25
   --budget-cap 500`. The first live order is also the first real test of the order
   code. Watch it in the Robinhood app.
4. **After ~100 closed trades (months, not weeks):** compare against the backtest.
   Stop if it's down more than 10%, or if it's losing more than it wins in dollars
   (profit factor below 1).

---

## 10. Running it on your home desktop

The bot uses almost no CPU. It needs to **stay up** and **keep accurate time**.

- **Linux:** run `sudo ./deploy/install_linux.sh`, fill in `/etc/rhbot.env`, then
  `sudo systemctl enable --now rhbot`. View logs with `journalctl -u rhbot -f`.
- **Windows:** use the Task Scheduler setup at the top of
  `deploy/windows/run_rhbot.ps1`, and store your keys as user environment variables.
- **Docker:** copy `.env.example` to `.env`, then run `docker compose up -d --build`.

**Machine checklist:**
- **Power:** turn off sleep. Set BIOS "Restore on AC power loss" to **On**.
- **Clock:** keep time sync on. Robinhood rejects requests more than ~30 seconds off.
- **Network:** a wired connection is best. The bot only makes outbound connections,
  so don't open router ports.
- **Phone alerts:** install the **ntfy** app, subscribe to a long random topic name,
  and set `RHBOT_NTFY_TOPIC` to that name. You'll get buys, sells, halts, errors and
  a daily summary.
- **If the PC goes down:** live positions keep their stop orders at Robinhood. When
  the bot restarts, it adopts them, and it re-places any stop that's missing.
- **Secrets:** keep the env file `chmod 600`. Never commit it.

---

## 11. Next steps

- A lower-cost exchange broker module. That's the biggest lever, by far.
- Read the actual fee tier from API v2 into the cost model.
- Walk-forward re-testing on your live fills after 3 months.
- More coins, which would make cross-sectional momentum possible.
