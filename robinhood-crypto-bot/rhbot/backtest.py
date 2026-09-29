"""Portfolio backtester that models what actually costs money on Robinhood.

- Signals are computed on bar i's close and filled at bar i+1's open.
- Every buy pays half the round-trip spread above mid, every sell half below,
  plus an optional explicit fee (API v2 fee tiers).
- Stops are checked against each bar's low; a gap through the stop fills at
  the open, not at the stop.
- Sizing and circuit breakers go through risk.py, same as live trading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from .data import Candle
from .risk import CircuitBreaker, RiskConfig, size_position
from .strategies import ENTER, EXIT, Strategy


@dataclass
class CostModel:
    spread_pct: float = 0.008   # round-trip, e.g. 0.8% => 0.4% each side
    fee_pct: float = 0.0        # explicit per-side fee, if any
    per_symbol_spread: dict[str, float] = field(default_factory=dict)

    def spread(self, symbol: str) -> float:
        return self.per_symbol_spread.get(symbol, self.spread_pct)

    def buy_price(self, symbol: str, mid: float) -> float:
        return mid * (1 + self.spread(symbol) / 2)

    def sell_price(self, symbol: str, mid: float) -> float:
        return mid * (1 - self.spread(symbol) / 2)


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float       # fill price incl. spread
    stop: float
    entry_ts: int
    bars: int = 0
    high_water: float = 0.0


@dataclass
class Trade:
    symbol: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    qty: float
    pnl: float
    costs: float
    reason: str

    @property
    def ret(self) -> float:
        return self.exit / self.entry - 1


@dataclass
class Result:
    equity_curve: list[tuple[int, float]]
    trades: list[Trade]
    start_equity: float
    bars_per_year: float
    halted: str = ""
    flows: list[float] = field(default_factory=list)  # deposit added at each curve point

    def metrics(self) -> dict:
        eq = [e for _, e in self.equity_curve]
        if len(eq) < 2:
            return {}
        flows = self.flows or [0.0] * len(eq)
        # Time-weighted returns: deposits are not performance.
        rets = [(eq[i] - flows[i]) / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
        contributed = self.start_equity + sum(flows)
        twr = 1.0
        for r in rets:
            twr *= 1 + r
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1))
        downside = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / len(rets))
        peak, mdd = eq[0], 0.0
        for e in eq:
            peak = max(peak, e)
            mdd = max(mdd, 1 - e / peak)
        years = len(rets) / self.bars_per_year
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        return {
            "final_equity": round(eq[-1], 2),
            "contributed": round(contributed, 2),
            "profit": round(eq[-1] - contributed, 2),
            "total_return": twr - 1,
            "cagr": twr ** (1 / years) - 1 if years > 0 and twr > 0 else 0.0,
            "sharpe": mean / sd * math.sqrt(self.bars_per_year) if sd else 0.0,
            "sortino": mean / downside * math.sqrt(self.bars_per_year) if downside else 0.0,
            "max_drawdown": mdd,
            "trades": len(self.trades),
            "win_rate": len(wins) / len(self.trades) if self.trades else 0.0,
            "profit_factor": gross_win / gross_loss if gross_loss else float("inf") if gross_win else 0.0,
            "avg_trade_ret": sum(t.ret for t in self.trades) / len(self.trades) if self.trades else 0.0,
            "costs_paid": sum(t.costs for t in self.trades),
            "halted": self.halted,
        }


def run(universe: dict[str, list[Candle]], strategy_factory: Callable[[], Strategy],
        risk: RiskConfig | None = None, costs: CostModel | None = None,
        start_equity: float = 10_000.0, bars_per_year: float = 24 * 365,
        daily_contribution: float = 0.0) -> Result:
    """`daily_contribution` adds that much cash at the first bar of each UTC
    day, like the live bot's daily allowance."""
    risk = risk or RiskConfig()
    costs = costs or CostModel()
    strats: dict[str, Strategy] = {}
    index: dict[str, dict[int, int]] = {}
    for sym, candles in universe.items():
        s = strategy_factory()
        s.prepare(candles)
        strats[sym] = s
        index[sym] = {c.ts: i for i, c in enumerate(candles)}
    timeline = sorted({c.ts for cs in universe.values() for c in cs})

    cash = start_equity
    positions: dict[str, Position] = {}
    pending: dict[str, str] = {}  # symbol -> ENTER/EXIT to fill at next open
    trades: list[Trade] = []
    curve: list[tuple[int, float]] = []
    breaker = CircuitBreaker(risk)
    last_close: dict[str, float] = {}
    flows: list[float] = []
    day = None

    def close_position(p: Position, mid: float, ts: int, reason: str) -> None:
        nonlocal cash
        px = costs.sell_price(p.symbol, mid)
        fee = px * p.qty * costs.fee_pct
        cash += px * p.qty - fee
        entry_mid = p.entry / (1 + costs.spread(p.symbol) / 2)
        cost = (p.entry - entry_mid) * p.qty + (mid - px) * p.qty + fee + p.entry * p.qty * costs.fee_pct
        trades.append(Trade(p.symbol, p.entry_ts, ts, p.entry, px, p.qty,
                            (px - p.entry) * p.qty - fee - p.entry * p.qty * costs.fee_pct,
                            cost, reason))
        del positions[p.symbol]

    def equity() -> float:
        return cash + sum(p.qty * last_close.get(s, p.entry) for s, p in positions.items())

    for ts in timeline:
        flow = 0.0
        if daily_contribution and ts // 86400 != day:
            day = ts // 86400
            cash += daily_contribution
            flow = daily_contribution
        # 1) fills at this bar's open for last bar's signals, then stops
        for sym in list(universe):
            i = index[sym].get(ts)
            if i is None:
                continue
            bar = universe[sym][i]
            act = pending.pop(sym, None)
            if act == EXIT and sym in positions:
                close_position(positions[sym], bar.open, ts, "signal")
            elif act == ENTER and sym not in positions and breaker.can_enter(equity()) and not breaker.halted:
                a = strats[sym].atr[i - 1]
                exposure = sum(p.qty * last_close.get(s, p.entry) for s, p in positions.items())
                px = costs.buy_price(sym, bar.open)
                size = size_position(risk, equity(), cash, exposure, len(positions),
                                     px, a or 0.0, costs.spread(sym))
                if size.ok:
                    qty = size.notional / px
                    cash -= px * qty * (1 + costs.fee_pct)
                    positions[sym] = Position(sym, qty, px, size.stop_price, ts, 0, bar.open)

            p = positions.get(sym)
            if p:
                if bar.low <= p.stop:
                    fill = min(bar.open, p.stop)  # gap through => fill at open
                    close_position(p, fill, ts, "stop")
                else:
                    p.bars += 1
                    p.high_water = max(p.high_water, bar.high)
                    a = strats[sym].atr[i]
                    if a and risk.trail_atr_mult:
                        p.stop = max(p.stop, p.high_water - risk.trail_atr_mult * a)
                    if risk.take_profit_r and a:
                        target = p.entry + risk.take_profit_r * risk.stop_atr_mult * a
                        if bar.high >= target:
                            close_position(p, max(target, bar.open), ts, "target")
            last_close[sym] = bar.close

        # 2) mark to market, circuit breakers
        eq = equity()
        breaker.update(ts, eq)
        curve.append((ts, eq))
        flows.append(flow)
        if breaker.halted:
            for p in list(positions.values()):
                close_position(p, last_close[p.symbol], ts, "halt")
            break

        # 3) new signals on this bar's close
        for sym in universe:
            i = index[sym].get(ts)
            if i is None or i < strats[sym].warmup:
                continue
            p = positions.get(sym)
            sig = strats[sym].signal(i, p is not None, p.bars if p else 0)
            if sig:
                pending[sym] = sig

    for p in list(positions.values()):
        close_position(p, last_close[p.symbol], timeline[-1], "end")
    if curve:
        curve[-1] = (curve[-1][0], cash)
    return Result(curve, trades, start_equity, bars_per_year, breaker.halt_reason, flows)
