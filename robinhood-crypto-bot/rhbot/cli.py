"""Command line: keygen, fetch, screen, backtest, trade.

    python -m rhbot keygen
    python -m rhbot fetch --interval 1h --bars 4000
    python -m rhbot screen [--synthetic]
    python -m rhbot backtest --strategy trend [--synthetic]
    python -m rhbot trade                 # paper trading (default)
    python -m rhbot trade --live          # real money; see README first
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from functools import partial
from pathlib import Path

from . import backtest, data, screener
from .risk import RiskConfig
from .strategies import STRATEGIES

DATA_DIR = Path(os.environ.get("RHBOT_DATA", "data"))
BARS_PER_DAY = {"1m": 1440, "5m": 288, "15m": 96, "1h": 24, "6h": 4, "1d": 1}


def _client():
    from .robinhood import RobinhoodClient

    key, secret = os.environ.get("RH_API_KEY"), os.environ.get("RH_PRIVATE_KEY")
    if not (key and secret):
        return None
    return RobinhoodClient(key, secret)


def _universe(args) -> dict[str, list[data.Candle]]:
    if args.synthetic:
        return data.synthetic_universe(args.symbols, bars=args.bars, interval=args.interval)
    out = {}
    for s in args.symbols:
        path = DATA_DIR / f"{s}_{args.interval}.csv"
        if not path.exists():
            print(f"fetching {s} {args.interval} from Coinbase...", file=sys.stderr)
            data.save_csv(data.fetch_coinbase(s, args.interval, args.bars), path)
        out[s] = data.load_csv(path)
    return out


def _spreads(args) -> dict[str, float]:
    """Live round-trip spreads from Robinhood when credentials are set."""
    client = None if args.synthetic else _client()
    if not client:
        return {}
    return {s: q.spread_pct for s, q in client.quotes(args.symbols).items()}


def _strategy_factory(name: str):
    return partial(STRATEGIES[name])


def cmd_keygen(args):
    from .robinhood import generate_keypair

    priv, pub = generate_keypair()
    print("Public key (paste into Robinhood > Account > Crypto > API trading):")
    print(f"  {pub}\n")
    print("Private key (put in RH_PRIVATE_KEY, never share or commit it):")
    print(f"  {priv}")


def cmd_fetch(args):
    for s in args.symbols:
        candles = data.fetch_coinbase(s, args.interval, args.bars)
        data.save_csv(candles, DATA_DIR / f"{s}_{args.interval}.csv")
        print(f"{s}: {len(candles)} bars")


def cmd_screen(args):
    uni = _universe(args)
    rows = screener.screen(uni, _spreads(args), bars_per_day=BARS_PER_DAY[args.interval],
                           strategy_factory=_strategy_factory(args.strategy),
                           default_spread=args.spread)
    print(f"{'symbol':10} {'score':>6} {'dVol':>7} {'spread':>7} {'vol/cost':>9} "
          f"{'effic':>6} {'btcCorr':>7} {'oosSharpe':>9} {'24h $vol':>12}")
    for r in rows:
        print(f"{r.symbol:10} {r.score:6.2f} {r.daily_vol:7.2%} {r.spread:7.2%} "
              f"{r.vol_to_cost:9.1f} {r.efficiency:6.2f} {r.btc_corr:7.2f} "
              f"{(r.oos_sharpe or 0):9.2f} {r.usd_volume_24h:12,.0f}")


def cmd_backtest(args):
    uni = _universe(args)
    costs = backtest.CostModel(spread_pct=args.spread, fee_pct=args.fee,
                               per_symbol_spread=_spreads(args))
    res = backtest.run(uni, _strategy_factory(args.strategy), RiskConfig(), costs,
                       start_equity=0.0 if args.daily_budget else args.equity,
                       bars_per_year=BARS_PER_DAY[args.interval] * 365,
                       daily_contribution=args.daily_budget)
    m = res.metrics()
    for k, v in m.items():
        pct = k in ("total_return", "cagr", "max_drawdown", "win_rate", "avg_trade_ret")
        print(f"{k:15} {v:.2%}" if pct else f"{k:15} {v:.2f}" if isinstance(v, float) else f"{k:15} {v}")
    by_sym: dict[str, list] = {}
    for t in res.trades:
        by_sym.setdefault(t.symbol, []).append(t)
    print("\nper coin:")
    for s, ts in sorted(by_sym.items()):
        print(f"  {s:10} trades {len(ts):4}  pnl ${sum(t.pnl for t in ts):10,.2f}  "
              f"costs ${sum(t.costs for t in ts):9,.2f}")


def cmd_trade(args):
    from .broker import PaperBroker, RobinhoodBroker
    from .budget import Budget
    from .trader import Trader

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
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
    budget = Budget(args.daily_budget) if args.daily_budget > 0 else None
    trader = Trader(broker, args.symbols, _strategy_factory(args.strategy), RiskConfig(),
                    args.interval, state, budget)
    if args.reset_halt:
        # Clears the kill switch and restarts drawdown tracking from current equity.
        trader.breaker.halted, trader.breaker.halt_reason, trader.breaker.peak_equity = False, "", 0.0
        logging.info("circuit breaker reset by operator")
    if args.once:
        for a in trader.step():
            print(a)
    else:
        mode = "LIVE" if args.live else "paper"
        logging.info(f"{mode} trading {', '.join(args.symbols)} on {args.interval} bars")
        trader.run_forever()


def main(argv=None):
    p = argparse.ArgumentParser(prog="rhbot")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, bars=4000):
        sp.add_argument("--symbols", nargs="+", default=screener.MAINSTREAM)
        sp.add_argument("--interval", default="1h", choices=list(BARS_PER_DAY))
        sp.add_argument("--bars", type=int, default=bars)
        sp.add_argument("--strategy", default="trend", choices=list(STRATEGIES))
        sp.add_argument("--spread", type=float, default=0.008,
                        help="assumed round-trip spread when no live quote (0.008 = 0.8%%)")
        sp.add_argument("--fee", type=float, default=0.0, help="explicit per-side fee fraction")
        sp.add_argument("--equity", type=float, default=10_000.0)
        sp.add_argument("--synthetic", action="store_true", help="use generated data (offline)")

    sub.add_parser("keygen").set_defaults(fn=cmd_keygen)
    for name, fn in [("fetch", cmd_fetch), ("screen", cmd_screen)]:
        sp = sub.add_parser(name)
        common(sp)
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("backtest")
    common(sp)
    sp.add_argument("--daily-budget", type=float, default=0.0,
                    help="simulate adding this much cash per day instead of starting with --equity")
    sp.set_defaults(fn=cmd_backtest)
    sp = sub.add_parser("trade")
    common(sp)
    sp.add_argument("--daily-budget", type=float, default=25.0,
                    help="the bot may use this much per day since it started, plus its own "
                         "profits (0 = use all account cash)")
    sp.add_argument("--live", action="store_true")
    sp.add_argument("--reset-halt", action="store_true",
                    help="clear a tripped drawdown kill switch (after you've looked at why)")
    sp.add_argument("--once", action="store_true", help="run one cycle and exit")
    sp.set_defaults(fn=cmd_trade)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
