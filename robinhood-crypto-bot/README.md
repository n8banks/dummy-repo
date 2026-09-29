# rhbot: a risk-managed crypto trader for Robinhood

A Python bot for the official **Robinhood Crypto Trading API**. It has four parts:

- a **coin screener** that ranks which coins are worth trading,
- a **backtester** that charges realistic costs,
- a **paper trader**, which is the default,
- a **live trader** behind two explicit safety switches.

> **Read this first.** Most retail traders who trade crypto frequently lose money. The
> main reason is costs, not bad signals. Treat this as a tool for testing ideas
> honestly: nothing here is a strategy that has been shown to make money. Run it in
> paper mode for weeks before you put money behind it, and only risk money you can
> afford to lose. None of this is financial advice.

---

## 1. The improved prompt

Here is your original request, rewritten into a spec that a developer (human or AI)
can build and test against:

> Build a Python trading system for the **official Robinhood Crypto Trading API**
> (`trading.robinhood.com`, Ed25519-signed requests) that trades liquid, mainstream
> spot crypto pairs. Start with BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, LINK, LTC and BCH
> against USD.
>
> **Goal:** profit from crypto's volatility with frequent but **cost-aware** trades.
> Only take a trade when the expected move clearly beats Robinhood's spread and any
> fees.
>
> **Requirements**
> 1. **Research:** document the API (auth, endpoints, order types, rate limits),
>    its real trading costs (spread plus fee tiers), and its limits: spot only, long
>    only, and no historical candles.
> 2. **Coin selection:** rank coins by volatility relative to trading cost, liquidity,
>    trend quality, correlation to BTC, and the strategy's out-of-sample performance.
>    Explain every score.
> 3. **Strategies:** at least one trend-following and one mean-reversion strategy.
>    Long only. Signals use completed bars only (no lookahead).
> 4. **Risk management:**
>    - size each position by volatility with a fixed fractional risk per trade,
>    - stops based on ATR, including a trailing stop,
>    - caps per position and on total exposure,
>    - a daily-loss pause and a max-drawdown kill switch,
>    - a native stop order on the exchange, so positions stay protected if the bot is
>      down.
> 5. **Backtesting:** simulate the whole portfolio, including spread and fees, fill
>    at the next bar's open, and fill stops that gap through at the open. Report
>    Sharpe, Sortino, max drawdown, win rate, profit factor and costs paid.
> 6. **Execution:** paper trading by default using live Robinhood quotes. Live trading
>    needs explicit opt-in. Respect the rate limits, keep order IDs idempotent, and
>    persist state so a restart is safe.
> 7. **Hosting:** run 24/7 on a home desktop (Linux service, Windows task, or Docker)
>    and recover from crashes and reboots.
> 8. **Tests** for request signing, order bodies, sizing, cost accounting and the
>    trading loop.
>
> 9. **Budget:** the bot gets **$25 per day** of capital, plus whatever it has made
>    or lost. It compounds its profits and never spends account cash beyond that.
> 10. **Many small bets:** no coin may take more than 10% of the bot's money, and up
>     to 8 positions can be open at once. Growth should come from many small
>     trades, not one big one.
> 11. **Loss cap:** no trade may lose more than **5%**. The stop always sits at most 5%
>     below the entry price.
> 12. **Deadline:** running by Friday.
>
> **Non-goals:** leverage, shorting, sub-minute high-frequency trading, and meme or
> micro-cap coins.

---

## 2. Research findings

### The Robinhood Crypto Trading API

Robinhood's docs site (`docs.robinhood.com/crypto/trading`) was unreachable from the
environment this was built in. The API details below were cross-checked against an
open-source client that implements the published spec
([nirholas/robinhood-mcp](https://github.com/nirholas/robinhood-mcp)).

| | |
|---|---|
| **Base URL** | `https://trading.robinhood.com` |
| **Auth** | Three headers: `x-api-key`, `x-timestamp` (unix seconds, valid about 30 s), and `x-signature`. The signature is Ed25519, base64-encoded, over `api_key + timestamp + path_with_query + METHOD + body`. The private key is a base64 **32-byte seed**. |
| **Rate limit** | 100 requests per minute, bursting to 300 |
| **Endpoints (v1)** | `trading/accounts/`, `trading/trading_pairs/`, `trading/holdings/`, `trading/orders/` (GET/POST), `trading/orders/{id}/cancel/`, `marketdata/best_bid_ask/`, `marketdata/estimated_price/` |
| **v2** | Same shape, plus fee-tier information. Needs an `account_number` query parameter, and `estimated_price` moves under `/trading/`. |
| **Order types** | `market` (only takes `asset_quantity`), `limit`, `stop_loss`, `stop_limit`. The last three take `asset_quantity` or `quote_amount`, plus `time_in_force`. |
| **Quotes** | `best_bid_ask` returns the mid `price` and **`bid_inclusive_of_sell_spread` / `ask_inclusive_of_buy_spread`**, which are the prices you actually get. The bot measures cost from these. |
| **Not available** | Historical candles, shorting, leverage, websockets |

Because the API has **no price history**, the bot gets candles from Coinbase's public
market-data API (no key needed) and gets costs and fills from Robinhood.

### Costs are what decide profitability

The consumer app charges no commission, but the spread is built into your price. Recent
guides put Robinhood's spread at roughly **0.35% to 0.85% for BTC and ETH**, and wider
for less liquid coins and in volatile markets. Robinhood also now publishes a **tiered
fee schedule** for its advanced and API trading surfaces, which is what API v2 exposes.
(Sources: [Robinhood crypto fee tiers](https://robinhood.com/us/en/support/articles/crypto-fee-tiers),
[Bitget: Robinhood crypto spreads 2026](https://www.bitget.com/academy/robinhood-crypto-trading-spreads-explained-2026-america-beginners-guide-costs-features-new-tools),
[Bitget: Robinhood crypto fees](https://www.bitget.com/academy/robinhood-crypto-fee).)
Check your own account's current numbers. `python -m rhbot screen` prints the live
spreads once your API key is set.

What this means for the design:

- **A round trip costs about 0.7% to 1.7%.** A scalping bot that aims for 0.3% moves
  loses money by construction. The bot trades **1-hour bars and holds for hours to
  days**, and only takes trades whose stop distance is several times the spread. That
  is `max_cost_to_stop` in `risk.py`.
- **Volatility only helps if it beats costs.** The screener's key number is
  `vol_to_cost`: the typical daily move divided by the round-trip spread.
- **Long only.** You can't short, so the realistic edges are (a) riding uptrends and
  sitting in cash during downtrends, and (b) buying sharp dips inside uptrends.

### Which coins

The default universe is the ten large, liquid coins listed above. The screener re-ranks
them using real data. On typical data, BTC and ETH have the tightest spreads but move
the least. SOL, DOGE and AVAX move the most, but with wider spreads. Most alts move
0.7 to 0.9 in step with BTC (correlation), so holding four of them at once is close to
one big BTC bet. That's why `max_exposure_pct` caps total crypto exposure, not just
each coin.

---

## 3. How it works

```
rhbot/
  robinhood.py   signed API client, token-bucket rate limiter, order-body builder
  data.py        Coinbase candle fetcher, CSV cache, synthetic data generator
  indicators.py  EMA/SMA/ATR/RSI/Donchian/volatility/correlation
  strategies.py  TrendBreakout (Donchian + EMA filter), MeanReversion (RSI + Bollinger in uptrend)
  risk.py        position sizing, 5% loss cap, exposure caps, daily-loss + drawdown breakers
  budget.py      the $25/day allowance ledger
  backtest.py    portfolio backtester with spread/fee/gap modelling
  screener.py    coin ranking
  broker.py      PaperBroker (JSON book, live quotes) / RobinhoodBroker (real orders + native stops)
  trader.py      the loop that runs once per closed bar
  cli.py         command-line entry point
```

**Default risk settings** (`RiskConfig` in `risk.py`):

- **Budget ($25 a day):** the bot may use $25 for each day since it first started,
  plus its realized profit or loss. Set this with `--daily-budget`; the start date
  is saved, so restarts don't reset it. You make the deposits into Robinhood
  yourself (a recurring transfer works). The bot only enforces its allowance.
  Coins you hold yourself in the same account are **never** sold.
- **Loss cap:** each trade's stop is placed at 2.5×ATR or **5% below the entry**,
  whichever is closer. So a trade is cut before it loses more than 5%. The
  exception is a sudden crash that jumps straight past the stop: the sell then
  fills at the next available price, which can be a bit worse.
- **Trailing stop:** 3×ATR, and it only ever moves up.
- **Many small bets:** each trade risks 0.5% of the bot's money. At most 10% goes
  into any one coin, 70% into crypto in total, and up to 8 positions can be open.
- **Minimum order:** $1. Robinhood has no per-order fee, so small orders cost the
  same percentage as big ones.
- **Circuit breakers:** new entries pause for the day after a 3% daily loss. After a
  15% drawdown from the peak, the bot sells its positions and halts until you run
  `trade --reset-halt`.

### What $25 a day and "1000 small trades" really means

- **Small trades spread out the risk, but they don't lower the cost.** Every trade
  still pays the spread, about 0.7% to 1.7% round trip. A thousand trades with no
  edge lose about a thousand spreads. The screener and the `max_cost_to_stop`
  filter exist to skip trades that can't pay for themselves.
- **At first, almost all growth will come from your deposits.** After 30 days the
  bot has been given $750. Even a very good strategy making 3% a month adds only
  about $20 on top of that. Compounding only matters once the pot is large, and
  only if the strategy has a real edge after costs.
- **Trade count:** on 1-hour bars across 10 coins, the trend strategy makes roughly
  2 to 5 trades a day. Reaching 1000 trades takes about 6 to 12 months. That also
  happens to be about how many trades you need before the results mean anything
  statistically.

### Plan for going live Friday

1. **Tue/Wed:** run `pip install -r requirements.txt`, `python -m rhbot fetch`,
   `python -m rhbot screen` and `python -m rhbot backtest --daily-budget 25` on real
   data. If the backtest loses money after costs, don't go live. Try `--strategy
   meanrev`, fewer coins, or a longer interval first.
2. **Wed:** generate a key with `python -m rhbot keygen`, register it with Robinhood,
   and set up the desktop (section 5). Start **paper** trading with
   `python -m rhbot trade`. Check `python -m rhbot trade --once` output against the
   Robinhood app.
3. **Fri:** switch to `--live` with `--daily-budget 25`. The most you can lose is what
   you've given the bot, and each trade is capped at -5%. Check the logs daily for
   the first week.

---

## 4. Usage

```bash
pip install -r requirements.txt        # just requests + cryptography

python -m rhbot backtest --synthetic   # offline smoke test on generated data
python -m rhbot fetch                  # download 4000 hourly bars per coin from Coinbase
python -m rhbot screen                 # rank coins (uses live RH spreads if keys are set)
python -m rhbot backtest --strategy trend
python -m rhbot backtest --strategy meanrev --spread 0.012   # test with wider spreads
python -m rhbot backtest --daily-budget 25                   # simulate the $25/day plan

python -m rhbot keygen                 # make an Ed25519 key pair for Robinhood
export RH_API_KEY=...  RH_PRIVATE_KEY=...
python -m rhbot trade --once           # one paper cycle, prints what it would do
python -m rhbot trade                  # paper trade continuously
```

To set up API access, run `python -m rhbot keygen`. Then register the **public** key
in Robinhood under **Account → Crypto → API Trading** and copy the API key it gives
you. Give the key the smallest permissions that work for you.

**Going live** needs both of these:

```bash
export RHBOT_LIVE_ACK="I understand this trades real money"
python -m rhbot trade --live
```

Start with a small account balance. The per-trade risk and exposure caps are
fractions of equity, so a small account means small orders.

### Things to do before trusting a backtest

1. Backtest on **real** data (`fetch`), not `--synthetic`. Synthetic data only
   checks that the code works; it says nothing about profits.
2. Keep the last third of the data aside and don't tune on it. The screener's
   `oos_sharpe` already does this.
3. Re-run with `--spread` set 50% higher than you measured. If the edge disappears,
   there wasn't one.
4. Paper trade for at least 2 to 4 weeks and compare the paper fills with the
   backtest.

---

## 5. Running it on your home desktop

An old gaming PC works fine. The bot uses almost no CPU: it wakes once an hour for a
few seconds. What matters is that it **stays up and keeps accurate time**.

**Pick one of these:**

- **Linux (recommended; install Ubuntu Server or run it alongside your current OS):**
  `sudo ./deploy/install_linux.sh`. Fill in `/etc/rhbot.env`, then run
  `sudo systemctl enable --now rhbot`, and view logs with `journalctl -u rhbot -f`.
  The service runs as a locked-down system user and restarts on crashes and at boot.
- **Windows:** `deploy/windows/run_rhbot.ps1` contains the one-time command that
  registers a Task Scheduler job. The job starts at boot and restarts on failure.
  Store your keys as user environment variables.
- **Docker (either OS):** put your keys in `.env`, then run
  `docker compose up -d --build`.

**Machine checklist:**

- **Power:** turn off sleep and hibernate. In the BIOS, set "Restore on AC power loss"
  to **On**. A cheap UPS covers short power outages.
- **Clock:** Robinhood rejects signatures more than about 30 seconds off. On Linux,
  `chrony` is installed by the script. On Windows, check that time sync is turned on.
- **Network:** use a wired connection if possible. The bot only makes **outbound**
  HTTPS requests, so don't open or forward any ports on your router.
- **Downtime:** entries and exits pause, but every live position has a native
  `stop_loss` order sitting at Robinhood. A crash is still protected against while
  the PC is off. On restart, the bot picks up its existing stops and holdings.
- **Secrets:** keep the env file readable only by you (`chmod 600`). Never commit
  `.env` or `/etc/rhbot.env`. Turn on full-disk encryption if others can use the PC.
- **Electricity:** a gaming desktop idles at roughly 60 to 120 W, which is about
  $6 to $15 a month. A mini PC or Raspberry Pi would do the same job for about $1 a
  month if that ever matters.

---

## 6. Next steps (not built yet)

- Walk-forward parameter search, with results reported out-of-sample only.
- Rank by correlation too, so the portfolio prefers coins that don't all move with
  BTC.
- Notifications on trades and halts (e.g. ntfy.sh or Discord webhook).
- Support API v2 fee tiers: read your fee tier from `/api/v2/crypto/trading/accounts/`
  and feed it into `CostModel.fee_pct`.
- Compare against simply buying and holding BTC. A strategy that can't beat that
  after costs isn't worth running.
