"""Command line.

    python -m rhbot keygen
    python -m rhbot fetch                    # daily history from Coinbase
    python -m rhbot research                 # compare strategies vs buy & hold
    python -m rhbot screen
    python -m rhbot backtest [--daily-budget 25]
    python -m rhbot trade --once             # one paper cycle
    python -m rhbot trade                    # paper trading (default)
    python -m rhbot trade --live             # real money; see README first
    python -m rhbot status
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import backtest, data, research, screener
from .risk import RiskConfig
from .strategies import STRATEGIES, build

DATA_DIR = Path(os.environ.get("RHBOT_DATA", "data"))
INTERVALS = {**data.GRANULARITIES, **data.RESAMPLED}


def _client():
    from .robinhood import RobinhoodClient

    key, secret = os.environ.get("RH_API_KEY"), os.environ.get("RH_PRIVATE_KEY")
    if not (key and secret):
        return None
    return RobinhoodClient(key, secret)


def _load(symbol: str, interval: str, bars: int) -> list[data.Candle]:
    if interval in data.RESAMPLED:
        hourly_bars = bars * data.RESAMPLED[interval] // 3600
        return data.resample(_load(symbol, "1h", hourly_bars), data.RESAMPLED[interval])
    path = DATA_DIR / f"{symbol}_{interval}.csv"
    if not path.exists():
        print(f"fetching {symbol} {interval} from Coinbase...", file=sys.stderr)
        data.save_csv(data.fetch_coinbase(symbol, interval, bars), path)
    return data.load_csv(path)


def _universe(args) -> dict[str, list[data.Candle]]:
    symbols = list(dict.fromkeys(args.symbols + ["BTC-USD"]))  # BTC drives the regime filter
    if args.synthetic:
        secs = INTERVALS[args.interval]
        hourly = data.synthetic_universe(symbols, bars=args.bars * secs // 3600, interval="1h")
        return hourly if secs == 3600 else {s: data.resample(c, secs) for s, c in hourly.items()}
    return {s: _load(s, args.interval, args.bars) for s in symbols}


def _traded(uni, args):
    return {s: uni[s] for s in args.symbols}


def _spreads(args) -> dict[str, float]:
    """Live round-trip spreads from Robinhood when credentials are set."""
    client = None if args.synthetic else _client()
    if not client:
        return {}
    return {s: q.spread_pct for s, q in client.quotes(args.symbols).items()}


def _builder(args):
    secs = INTERVALS[args.interval]
    return lambda uni: build(args.strategy, secs, None if args.no_regime else uni.get("BTC-USD"))


def _risk(args) -> RiskConfig:
    return RiskConfig(max_stop_pct=args.max_loss, hold_to_profit=tuple(args.hold_to_profit),
                      hold_floor=args.hold_floor)


def _bars_per_year(interval: str) -> float:
    return 365 * 86400 / INTERVALS[interval]


def cmd_keygen(args):
    from .robinhood import generate_keypair

    priv, pub = generate_keypair()
    print("Public key (paste into Robinhood > Account > Crypto > API trading):")
    print(f"  {pub}\n")
    print("Private key (put in RH_PRIVATE_KEY, never share or commit it):")
    print(f"  {priv}")


def cmd_fetch(args):
    for s in dict.fromkeys(args.symbols + ["BTC-USD"]):
        interval = "1h" if args.interval in data.RESAMPLED else args.interval
        candles = data.fetch_coinbase(s, interval, args.bars)
        data.save_csv(candles, DATA_DIR / f"{s}_{interval}.csv")
        print(f"{s}: {len(candles)} {interval} bars")


def cmd_screen(args):
    uni = _universe(args)
    rows = screener.screen(_traded(uni, args), _spreads(args),
                           bars_per_day=max(1, 86400 // INTERVALS[args.interval]),
                           strategy_factory=_builder(args)(uni), risk=_risk(args),
                           default_spread=args.spread)
    print(f"{'symbol':10} {'score':>6} {'dVol':>7} {'spread':>7} {'vol/cost':>9} "
          f"{'effic':>6} {'btcCorr':>7} {'oosSharpe':>9} {'24h $vol':>14}")
    for r in rows:
        print(f"{r.symbol:10} {r.score:6.2f} {r.daily_vol:7.2%} {r.spread:7.2%} "
              f"{r.vol_to_cost:9.1f} {r.efficiency:6.2f} {r.btc_corr:7.2f} "
              f"{(r.oos_sharpe or 0):9.2f} {r.usd_volume_24h:14,.0f}")


def cmd_backtest(args):
    uni = _universe(args)
    costs = backtest.CostModel(spread_pct=args.spread, fee_pct=args.fee,
                               per_symbol_spread=_spreads(args))
    res = backtest.run(_traded(uni, args), _builder(args)(uni), _risk(args), costs,
                       start_equity=0.0 if args.daily_budget else args.equity,
                       bars_per_year=_bars_per_year(args.interval),
                       daily_contribution=args.daily_budget, cash_apy=args.cash_apy)
    for k, v in res.metrics().items():
        pct = k in ("total_return", "cagr", "max_drawdown", "win_rate", "avg_trade_ret")
        print(f"{k:15} {v:.2%}" if pct else f"{k:15} {v:.2f}" if isinstance(v, float) else f"{k:15} {v}")
    print("\nper year:  " + "  ".join(f"{y} {r:+.1%}" for y, r in sorted(
        research.yearly(res.equity_curve, res.flows).items())))
    bh = research.buy_and_hold(_traded(uni, args), args.spread)
    s = research.curve_stats(bh, _bars_per_year(args.interval))
    print(f"buy & hold (equal weight, same period): CAGR {s['cagr']:.1%}, "
          f"max drawdown {s['max_drawdown']:.1%}, Sharpe {s['sharpe']:.2f}")
    by_sym: dict[str, list] = {}
    for t in res.trades:
        by_sym.setdefault(t.symbol, []).append(t)
    print("\nper coin:")
    for sym, ts in sorted(by_sym.items()):
        print(f"  {sym:10} trades {len(ts):4}  pnl ${sum(t.pnl for t in ts):10,.2f}  "
              f"costs ${sum(t.costs for t in ts):9,.2f}")


def cmd_research(args):
    uni = _universe(args)
    secs = INTERVALS[args.interval]
    btc = uni["BTC-USD"]
    variants = {}
    for name in ("trend", "tsmom"):
        variants[name] = build(name, secs)
        variants[f"{name}+regime"] = build(name, secs, btc)
    rows = research.compare(_traded(uni, args), variants, args.spread, _bars_per_year(args.interval),
                            _risk(args))
    research.print_table(rows)


def _trader(args):
    from .broker import PaperBroker, RobinhoodBroker
    from .budget import Budget
    from .trader import Trader

    client = _client()
    if args.live:
        if not client:
            sys.exit("--live needs RH_API_KEY and RH_PRIVATE_KEY")
        if os.environ.get("RHBOT_LIVE_ACK") != "I understand this trades real money":
            sys.exit('Set RHBOT_LIVE_ACK="I understand this trades real money" to trade live.')
        broker = RobinhoodBroker(client, args.symbols)
        state = DATA_DIR / "live_state.json"
    else:
        broker = PaperBroker(DATA_DIR / "paper_book.json", args.equity, client, args.spread)
        state = DATA_DIR / "paper_state.json"
    budget = Budget(args.daily_budget, cap=args.budget_cap) if args.daily_budget > 0 else None
    # Enough history for the longest lookback (90 days) plus the 100-day regime EMA.
    history = max(200, 130 * 86400 // INTERVALS[args.interval])
    return Trader(broker, args.symbols, _builder(args), _risk(args), args.interval, state, budget,
                  history_bars=history)


def cmd_trade(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    trader = _trader(args)
    if args.reset_halt:
        # Clears the kill switch and restarts drawdown tracking from current equity.
        trader.breaker.halted, trader.breaker.halt_reason, trader.breaker.peak_equity = False, "", 0.0
        trader._save()
        logging.info("circuit breaker reset by operator")
    if args.once:
        for a in trader.step():
            print(a)
        return
    mode = "LIVE" if args.live else "paper"
    logging.info(f"{mode} trading {', '.join(args.symbols)} on {args.interval} bars, "
                 f"strategy {args.strategy}{'' if args.no_regime else '+regime'}")
    trader.run_forever()


def cmd_status(args):
    trader = _trader(args)
    print(trader.summary())
    for sym, p in trader.state["positions"].items():
        print(f"  {sym:10} qty {p['qty']:.8g} entry {p['entry']:.6g} stop {p['stop']:.6g} "
              f"held {p['bars']} bars")
    if trader.breaker.halted:
        print(f"HALTED: {trader.breaker.halt_reason}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="rhbot")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--symbols", nargs="+", default=screener.MAINSTREAM)
        sp.add_argument("--interval", default="1d", choices=list(INTERVALS),
                        help="bar size; real-data tests favour 1d (see README)")
        sp.add_argument("--bars", type=int, default=2000)
        sp.add_argument("--strategy", default="tsmom", choices=list(STRATEGIES))
        sp.add_argument("--no-regime", action="store_true",
                        help="don't require BTC to be in an uptrend before buying")
        sp.add_argument("--max-loss", type=float, default=0.15,
                        help="widest stop, as a fraction below entry (0.15 = 15%%). Positions are "
                             "sized so a stopped trade still loses ~0.5%% of the pot")
        sp.add_argument("--hold-to-profit", nargs="*", default=[], metavar="SYMBOL",
                        help="never sell these below a small profit (tested: worse; see README)")
        sp.add_argument("--hold-floor", type=float, default=0.0,
                        help="with --hold-to-profit, still sell if down this much (0.4 = 40%%)")
        sp.add_argument("--spread", type=float, default=0.008,
                        help="assumed round-trip spread when no live quote (0.008 = 0.8%%)")
        sp.add_argument("--fee", type=float, default=0.0, help="explicit per-side fee fraction")
        sp.add_argument("--equity", type=float, default=10_000.0)
        sp.add_argument("--synthetic", action="store_true", help="use generated data (offline)")

    sub.add_parser("keygen").set_defaults(fn=cmd_keygen)
    for name, fn in [("fetch", cmd_fetch), ("screen", cmd_screen), ("research", cmd_research)]:
        sp = sub.add_parser(name)
        common(sp)
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("backtest")
    common(sp)
    sp.add_argument("--daily-budget", type=float, default=0.0,
                    help="simulate adding this much cash per day instead of starting with --equity")
    sp.add_argument("--cash-apy", type=float, default=0.0,
                    help="interest earned on idle cash, e.g. 0.04 for a 4%% sweep rate")
    sp.set_defaults(fn=cmd_backtest)
    for name, fn in [("trade", cmd_trade), ("status", cmd_status)]:
        sp = sub.add_parser(name)
        common(sp)
        sp.add_argument("--daily-budget", type=float, default=25.0,
                        help="the bot may use this much per day since it started, plus its own "
                             "profits (0 = use all account cash)")
        sp.add_argument("--budget-cap", type=float, default=500.0,
                        help="stop growing the allowance once this much has been given (0 = no cap)")
        sp.add_argument("--live", action="store_true")
        sp.add_argument("--reset-halt", action="store_true",
                        help="clear a tripped drawdown kill switch (after you've looked at why)")
        sp.add_argument("--once", action="store_true", help="run one cycle and exit")
        sp.set_defaults(fn=fn)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
