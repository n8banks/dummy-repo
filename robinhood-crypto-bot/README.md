# rhbot: a risk-managed crypto trend follower for Robinhood

A Python bot for the official **Robinhood Crypto Trading API**. It includes:

- a daily trend-following strategy with a BTC "market regime" filter,
- a strict budget ledger,
- stop-loss orders that rest at Robinhood (native stops),
- a backtester and research harness that charge realistic costs,
- a paper trader (the default),
- a live trader behind two explicit safety switches.

> **Bottom line, after independent review:** this is a real but **thin** edge at
> Robinhood's prices.
> - On 2018–2021 data that no setting was chosen on, it made about +18%/yr with a
>   ~9% worst drop.
> - From 2022 to 2026, at Robinhood's real ~2% round-trip cost, it roughly broke
>   even.
>
> Its strength is sitting out crashes, not beating buy-and-hold. Only risk money you
> can lose. None of this is financial advice.

---

## 1. The spec (your prompt, improved)

> Build a Python trading system for the **official Robinhood Crypto Trading API**.
> It trades liquid, mainstream spot crypto (BTC, ETH, SOL, XRP, DOGE, ADA, AVAX,
> LINK, LTC, BCH vs USD).
>
> - **Goal:** grow a small, regularly topped-up pot through many small, cost-aware
>   trades.
> - **Budget:** $25/day of new capital, capped at $500 to start. The bot compounds
>   its profits and never spends other account cash or touches coins or orders you
>   own yourself.
> - **Diversification:** at most 10% of the pot per coin and 8 positions.
> - **Loss control:**
>   - Stop at most 15% below entry, with each trade sized so a stop-out costs
>     about 0.5% of the pot.
>   - Always sell losers.
>   - After a 3% daily loss, pause new buys.
>   - After a 20% drawdown, sell everything and halt until a human resets it.
> - **Evidence:** real historical data, realistic costs (spread, fees, stop
>   slippage), comparison with buy-and-hold, and data held back that no setting
>   was chosen on.
> - **Operations:**
>   - Paper trading by default; live needs explicit opt-in.
>   - Crash-safe: every order is recorded before it's sent, and state is saved
>     after every fill.
>   - Runs 24/7 on a home desktop with phone alerts.

---

## 2. Robinhood API and real costs

| | |
|---|---|
| **Base URL** | `https://trading.robinhood.com`. There's no sandbox, so testing means real orders. |
| **Auth** | Headers `x-api-key`, `x-timestamp` and `x-signature`. The signature is Ed25519 over `api_key + timestamp + path_with_query + METHOD + body`, using a base64 32-byte seed as the private key. Timestamps expire after about 30 s. |
| **Rate limit** | 100 requests/min, bursting to 300 |
| **Endpoints** | `trading/{accounts,trading_pairs,holdings,orders}/`, `orders/{id}/cancel/`, `marketdata/{best_bid_ask,estimated_price}/` |
| **Orders** | `market` (asset quantity only), `limit`, `stop_loss` (becomes a market order when triggered), `stop_limit`. `time_in_force` is gtc, gfd, gfw or gfm. |
| **Not available** | Price history (candles come from Coinbase), shorting, leverage |

**What it really costs.** There are two ways to pay, and at small size they cost
about the same:
- **API v1:** no fee, but Robinhood takes about $0.95 per $100 through the spread
  on each side.
- **API v2:** an explicit fee of about 0.95% per side at the lowest volume tier,
  on a near-zero spread.

That's **about 1.9–2% per round trip.** The backtests default to 2% plus 0.5%
slippage on stop fills.

Robinhood's docs were blocked from the build environment. These details come from a
third-party copy of the published spec and from open-source clients:
- [spec copy](https://github.com/mvanhorn/printing-press-library/blob/HEAD/library/payments/robinhood/spec.json)
- [keel](https://github.com/CodeGateSoftware/keel)
- [nirholas/robinhood-mcp](https://github.com/nirholas/robinhood-mcp)
- [RH order routing](https://robinhood.com/us/en/support/articles/crypto-order-routing/)
- [RH fee tiers](https://robinhood.com/us/en/support/articles/crypto-fee-tiers)

**The live code has never been run against the real API**, so your first order is
its first real test.

---

## 3. Evidence

**Data:** real daily candles, Aug 2017 to Sep 2026, from Binance via the
public-domain [Speirsy11/crypto-dataset](https://github.com/Speirsy11/crypto-dataset).
Coins: BTC, ETH, SOL, XRP, DOGE, ADA, BCH.

**Default strategy:** momentum with regime filter, 15% stop cap, 0.5% risk per
trade. Costs: 2% round trip plus 0.5% stop slippage. The kill switch is off in
these tables so the full history is visible.

| Period | CAGR | Sharpe | Worst drop | Trades/yr |
|---|---|---|---|---|
| **2018–2021** (unseen; no setting was chosen on it) | **+18.0%** | **1.27** | 9.4% | 56 |
| 2022–Sep 2026 | +0.6% | 0.12 | 18.3% | 59 |
| **2018–Sep 2026** | **+8.2%** | **0.75** | 18.3% | 58 |
| Buy & hold BTC, 2018–2026 | +23.5% | 0.65 | 81% | — |
| Buy & hold ETH, 2018–2026 | +15.6% | 0.60 | 94% | — |

(CAGR = average yearly growth. Sharpe = return per unit of risk; higher is better.
"Regime" = only buy while BTC is above its 100-day average.)

**What it does well:** it avoided the 2018 and 2022 crashes (−1% and −3% while
coins fell 65–83%) and caught the 2020–21 and 2023–24 trends.

**What it does badly:** it bleeds slowly in choppy markets, and at 2% per round
trip that bleed ate all of 2022–26's gains.

**Your plan, simulated:** $25/day up to $500, 20% kill switch, starting from each of
24 dates between 2018 and 2025, measured to Sep 2026:

| Venue (cost) | Median | Worst start | Best start | Losing starts |
|---|---|---|---|---|
| Robinhood (~2%) | **+2.4%/yr** | −8.3%/yr | +11.3%/yr | 5/24 |
| Kraken Pro-like (~0.8%) | **+6.3%/yr** | −6.0%/yr | +16.3%/yr | 4/24 |

The 24 starts share much of the same history, so they aren't 24 independent
trials.

### How the settings were chosen

- **Daily bars, not hourly.** Hourly and 4-hour versions lost 7–12%/yr on real
  data even at 0.8% costs. Frequent trading just pays the spread more often.
- **Regime filter.** Blocking buys while BTC is below its 100-day average cut
  2022's loss from −9% to −3%. When the audit shifted the signal a day *later*,
  results got worse, and a day *earlier* (a deliberate peek at the future) they got
  better. That's the pattern expected when the filter isn't secretly using future
  data.
- **15% stop cap.** Tested from 3% to 30% on 2022–24, checked on 2025–26 and again
  on 2018–21. 15–20% is a plateau.
  - Tight stops (3–5%) whipsaw: they sell on normal swings and buy back higher.
  - Position size shrinks as the stop widens, so the cap mostly works as a size
    dial. Per-trade risk stays at about 0.5% of the pot.
- **Stickier exits.** The bot sells only when at most 1 of 5 lookbacks still says
  "up". That means fewer round trips, and it was equal or better in every period
  at 2% costs.
- **20% kill switch.** A 15% switch halted almost every simulation during the same
  normal 2025 drawdown (about −15 to −18%). 20% still catches a broken strategy.
- **Risk per trade 0.5%.** Raising it to 0.75% adds about half again to returns, but
  it deepened 2022–26's worst drop from 15% to 22%. It's a reasonable growth
  setting only after live results justify it.

### Rejected: "hold BTC/ETH until profitable"

The same plan with a rule to never sell below entry gave:
- lower returns,
- a win rate of 67% instead of about 30%, which *felt* better but made less money,
- positions stuck underwater for up to 685 days (ETH was still below its Jan 2022
  price in Sep 2026),
- a kill switch that fired anyway, selling at the worst moment.

It's still available as `--hold-to-profit BTC-USD ETH-USD`, but I don't recommend
it.

---

## 4. Independent review (three separate reviewers)

**1. Backtest audit.** It tried to break the backtester and found **no look-ahead
bug**.
- Accounting reconciles to the cent.
- Random entries with the same exits never matched the strategy in 30 tries.
- Zero-trend synthetic data produces no profit, so the code doesn't invent an edge.

It did find optimism, all now fixed or disclosed:
- Stops used to fill exactly at the stop price; there's now 0.5% slippage.
- Deposits hid drawdowns; drawdown is now measured on a deposit-adjusted index,
  in both the backtest and the live kill switch.
- The daily-loss pause never fired on daily bars; it now compares against about
  24 hours earlier.
- The cash-interest boost was overstated; see section 5.
- Settings were chosen on 2022–26 data; hence the separate 2018–21 test.
- Survivorship: the coins were picked in 2026, and Robinhood reportedly delisted
  SOL and ADA in 2023 (unverified). Using only BTC, ETH, DOGE and BCH gives about
  +4.7%/yr for 2018–26.

**2. Live-code review (two rounds).** It found real money-losing bugs. All are now
fixed and covered by fake-exchange tests in `tests/test_live.py`:

| Bug | Fix |
|---|---|
| It could sell coins you own yourself after a native stop filled, or when an order's response was lost or slow | The bot tracks only its own quantity and order IDs, and nothing new is placed for a coin while an earlier order's outcome is unknown |
| Moving a stop could leave the position unprotected (cancels settle asynchronously) | It waits for the cancel to settle; if the stop filled first, it books that fill; if a new stop can't be placed, it restores protection or closes |
| A lost response could place a second order or lose track of a fill | Order requests are never auto-retried; every order is recorded before it's sent and matched by its ID afterwards, and a resting stop found this way is adopted, not duplicated |
| Partly filled stops could be booked twice | Only the newly filled quantity is booked |
| State was saved only once per cycle | It's saved after every fill, together with clearing the order record |
| It cancelled or adopted your own open orders | It no longer touches orders it didn't place |
| One error could skip the day's stop checks | Errors are caught per coin, and stop checks run even when the daily cycle fails |
| Two running copies could both trade | A lock allows only one copy at a time |

42 tests pass. Paper trading can't produce lost responses or partial fills, so
those paths are tested only against the fake exchange.

**3. Cost research.** This established the ~2% round-trip cost above. The same
strategy on Kraken Pro (0.25% maker / 0.40% taker, with native stop orders) roughly
**2.5× the median return** in the plan simulation.

---

## 5. Is this an income stream?

Not at this size, and it's worth being blunt:

- **$500 at about 2–6% a year is about $10–30 a year.** Even the best simulated
  start (+11%/yr) is about $55 a year.
- **The deposits are the wealth builder.** $25/day is about $9,000 a year.
- **Robinhood Gold's cash sweep (3.6% APY as of Sep 2026) earns more, more
  reliably, than the trading does at this size.** The bot holds cash about 85% of
  the time, so that cash keeps earning while it waits. The interest is paid to
  your account, not counted in the bot's ledger. At $500, Gold's $5/month fee
  costs more than the interest; since you already have Gold, it's a free bonus.
- **If you want the edge to matter, the lever is cost, not tuning.** A Kraken
  broker module (0.5–0.8% round trip) is the highest-value next build.
- Other low-effort yield on Robinhood: ETH/SOL staking (from $1; Robinhood keeps
  25% of rewards; not offered in every state).

---

## 6. How it works

```
rhbot/
  robinhood.py   signed API client (reads retried, orders never), rate limiter
  broker.py      PaperBroker (simulated exchange) / RobinhoodBroker: every order
                 by client_order_id, polled until done; native stop-loss orders
  trader.py      daily decision loop + stop check every 15 minutes; write-ahead
                 order log, reconciliation, per-fill saves, phone alerts
  risk.py        sizing, 15% stop cap, deposit-adjusted circuit breakers,
                 hold-to-profit option
  strategies.py  TSMomentum (default), TrendBreakout, MeanReversion, RegimeFilter
  budget.py      $25/day allowance with a cap
  backtest.py    portfolio backtest: spread, fees, stop slippage, gaps,
                 deposits, cash interest
  research.py    variant comparison vs buy & hold, per-year returns
  data.py, indicators.py, screener.py, util.py
```

**Strategy:** each coin gets a vote from 5 lookbacks: is it higher than it was 7,
14, 30, 60 and 90 days ago?
- **Buy** when at least 4 of 5 say "up" **and** BTC is above its 100-day average.
- **Sell** when at most 1 of 5 still says "up", or when the trailing stop is hit.

**Risk defaults:**
- Stop at 2.5×ATR (ATR is the average daily price range) or 15% below entry,
  whichever is closer. Trailing stop at 3×ATR.
- Each trade sized so a stop-out costs about 0.5% of the pot.
- At most 10% per coin, 70% invested in total, 8 positions.
- Pause new buys after a 3% daily loss; halt at a 20% drawdown.

---

## 7. Usage

```bash
pip install -r requirements.txt

python -m rhbot fetch                        # ~5 years of daily candles from Coinbase
python -m rhbot research                     # strategy comparison vs buy & hold
python -m rhbot backtest --daily-budget 25   # defaults: 2% cost + 0.5% stop slippage
python -m rhbot backtest --equity 500 --spread 0.008   # what a cheaper venue would do
python -m rhbot screen                       # live Robinhood spreads once keys are set

python -m rhbot keygen                       # key pair for Robinhood API access
python -m rhbot trade --once                 # one paper cycle
python -m rhbot trade                        # paper trade continuously
python -m rhbot status                       # pot, positions, stops, realized P&L
```

**Going live** requires `RHBOT_LIVE_ACK="I understand this trades real money"` in
the environment **and** the `--live` flag.

**API keys:** register the **public** key from `keygen` in Robinhood (Account →
Crypto → API Trading). Put the API key it gives you, plus your private key, only in
the env file on your desktop. Never paste them into a chat, a commit or a
screenshot.

---

## 8. Plan

1. **Set up the desktop** (section 9). Run `fetch`, `research` and
   `backtest --daily-budget 25` there, on up-to-date Coinbase data.
2. **Paper trade** (the default) and check `status` against what you'd expect.
3. **Run `screen` with keys set** to see the costs you actually pay.
   - If the round trip is well above 2%, don't go live.
   - Consider the v2 API (explicit fees, counts toward volume tiers).
4. **Supervised live trial:**
   - Fund $500 and add `--live`.
   - The first orders are the first real test of the order code, so watch them in
     the app.
   - Check that every buy gets a stop order.
   - Act on any "uncertain" or "error" phone alert.
   - For the first weeks, keep coins you hold yourself out of this account.
5. **Judge it after about 100 closed trades,** which takes about 1.5 years at ~60
   trades a year.
   - Stop if it's down more than 10%, or if losses exceed gains in dollars.
   - Expect it to lag buy-and-hold in bull markets. That's the price of sitting out
     crashes.

---

## 9. Running it on your home desktop

The bot uses almost no CPU. It needs to **stay up** and **keep accurate time**.

- **Linux:** run `sudo ./deploy/install_linux.sh`, fill in `/etc/rhbot.env`, then
  `sudo systemctl enable --now rhbot`. View logs with `journalctl -u rhbot -f`.
- **Windows:** use the Task Scheduler setup at the top of
  `deploy/windows/run_rhbot.ps1`, and store your keys as user environment variables.
- **Docker:** copy `.env.example` to `.env`, then run `docker compose up -d --build`.

**Machine checklist:**
- **Power:** turn off sleep. Set BIOS "Restore on AC power loss" to **On**.
- **Clock:** keep time sync on. Robinhood rejects requests more than ~30 s off.
- **Network:** wired is best. The bot only makes outbound connections, so don't
  open router ports.
- **Phone alerts:** install the **ntfy** app, subscribe to a long random topic name,
  and set `RHBOT_NTFY_TOPIC` to it. You'll get buys, sells, halts, errors and a
  daily summary.
- **If the PC goes down:** native stops keep protecting positions at Robinhood. On
  restart, the bot polls its own stop orders and books any fills. Only one copy
  can run at a time.
- **Secrets:** keep the env file `chmod 600`. Never commit it.

---

## 10. Next steps

- **A Kraken Pro broker module.** It cuts costs 2.5–4×, which is the biggest lever
  by far.
- API v2 support, with your real fee tier fed into the cost model.
- More coins. Research suggests trend following works best with about 10–15 coins.
- After 3 months of live data, compare real fills with the backtest.
