"""Compare strategy variants on the same data, honestly.

For every variant: full-period metrics with the drawdown kill switch
disabled (so we see the whole history, not just the first bad month),
returns per calendar year, and the same numbers for buying and holding.
A variant is only interesting if it beats doing nothing on risk-adjusted
terms after costs, and does so in most years rather than one lucky one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Callable

from . import backtest
from .data import Candle
from .risk import RiskConfig
from .strategies import Strategy


def yearly(curve: list[tuple[int, float]], flows: list[float] | None = None) -> dict[int, float]:
    """Time-weighted return per calendar year."""
    flows = flows or [0.0] * len(curve)
    out: dict[int, float] = {}
    growth: dict[int, float] = {}
    for i in range(1, len(curve)):
        y = dt.datetime.fromtimestamp(curve[i][0], dt.timezone.utc).year
        prev = curve[i - 1][1]
        if prev > 0:
            growth[y] = growth.get(y, 1.0) * ((curve[i][1] - flows[i]) / prev)
    for y, g in growth.items():
        out[y] = g - 1
    return out


def buy_and_hold(universe: dict[str, list[Candle]], spread: float,
                 symbols: list[str] | None = None) -> list[tuple[int, float]]:
    """Equal-weight buy at the first common bar, pay half the spread, never sell."""
    symbols = symbols or list(universe)
    start = max(universe[s][0].ts for s in symbols)
    series = {s: {c.ts: c.close for c in universe[s] if c.ts >= start} for s in symbols}
    first = {s: series[s][start] for s in symbols}
    w = 1 / len(symbols) * (1 - spread / 2)
    last = dict(first)
    curve = []
    for ts in sorted(set().union(*[set(v) for v in series.values()])):
        for s in symbols:
            last[s] = series[s].get(ts, last[s])
        curve.append((ts, 10_000 * sum(w * last[s] / first[s] for s in symbols)))
    return curve


def curve_stats(curve: list[tuple[int, float]], bars_per_year: float) -> dict:
    r = backtest.Result(curve, [], curve[0][1], bars_per_year)
    m = r.metrics()
    return {k: m[k] for k in ("total_return", "cagr", "sharpe", "max_drawdown")}


def compare(universe: dict[str, list[Candle]], variants: dict[str, Callable[[], Strategy]],
            spread: float, bars_per_year: float, risk: RiskConfig | None = None,
            stop_slippage: float = 0.005) -> list[dict]:
    risk = replace(risk or RiskConfig(), max_drawdown=0.999)  # observe, don't halt
    rows = []
    for name, factory in variants.items():
        res = backtest.run(universe, factory, risk, backtest.CostModel(spread_pct=spread, stop_slippage=stop_slippage),
                           bars_per_year=bars_per_year)
        m = res.metrics()
        m["name"] = name
        m["yearly"] = yearly(res.equity_curve)
        m["exposure"] = _avg_exposure(res, universe)
        rows.append(m)
    for label, syms in (("buy&hold all", None), ("buy&hold BTC", ["BTC-USD"])):
        if syms and syms[0] not in universe:
            continue
        c = buy_and_hold(universe, spread, syms)
        m = curve_stats(c, bars_per_year)
        m.update(name=label, trades=len(syms or universe), profit_factor=float("nan"),
                 win_rate=float("nan"), costs_paid=float("nan"), yearly=yearly(c), exposure=1.0)
        rows.append(m)
    return rows


def _avg_exposure(res: backtest.Result, universe) -> float:
    """Rough share of time with at least one position open."""
    if not res.trades or not res.equity_curve:
        return 0.0
    span = res.equity_curve[-1][0] - res.equity_curve[0][0]
    held = 0
    intervals = sorted((t.entry_ts, t.exit_ts) for t in res.trades)
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if s > cur_e:
            held += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    held += cur_e - cur_s
    return held / span if span else 0.0


def print_table(rows: list[dict]) -> None:
    years = sorted({y for r in rows for y in r["yearly"]})
    head = f"{'variant':28} {'CAGR':>7} {'Sharpe':>6} {'maxDD':>6} {'PF':>5} {'trades':>6} {'inMkt':>5}"
    print(head + "".join(f" {y:>7}" for y in years))
    for r in rows:
        line = (f"{r['name']:28} {r['cagr']:7.1%} {r['sharpe']:6.2f} {r['max_drawdown']:6.1%} "
                f"{r['profit_factor']:5.2f} {r['trades']:6} {r['exposure']:5.0%}")
        print(line + "".join(f" {r['yearly'].get(y, float('nan')):7.1%}" for y in years))
