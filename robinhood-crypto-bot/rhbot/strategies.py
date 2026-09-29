"""Long-only signal generators. Robinhood crypto is spot-only (no shorting,
no leverage), so every strategy here only decides when to be long and when
to be flat.

A strategy precomputes its indicators once over the whole candle list, then
`signal(i)` looks only at bars 0..i. The caller acts on the *next* bar, so
nothing here can see the future.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import indicators as ind
from .data import Candle

ENTER, EXIT = "enter", "exit"


class Strategy:
    name = "base"
    warmup = 0

    def prepare(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.closes = [c.close for c in candles]
        self.atr = ind.atr(candles, 14)

    def signal(self, i: int, in_position: bool, bars_held: int) -> str | None:
        raise NotImplementedError


@dataclass
class TrendBreakout(Strategy):
    """Buy a close above the prior N-bar high while the fast EMA is above the
    slow EMA; exit on a close below the prior N/2-bar low (plus the trailing
    stop the backtester/trader manage). Classic Donchian/turtle trend-follow:
    low win rate, relies on a few large winners, which is what volatile
    trending coins provide."""

    entry_n: int = 48
    exit_n: int = 24
    fast: int = 20
    slow: int = 100
    name: str = "trend"

    @property
    def warmup(self) -> int:  # type: ignore[override]
        return max(self.entry_n, self.slow) + 1

    def prepare(self, candles):
        super().prepare(candles)
        self.hi, _ = ind.donchian(candles, self.entry_n)
        _, self.lo = ind.donchian(candles, self.exit_n)
        self.ef = ind.ema(self.closes, self.fast)
        self.es = ind.ema(self.closes, self.slow)

    def signal(self, i, in_position, bars_held):
        c = self.closes[i]
        if not in_position:
            if self.hi[i] and self.ef[i] and self.es[i] and c > self.hi[i] and self.ef[i] > self.es[i]:
                return ENTER
        elif self.lo[i] and c < self.lo[i]:
            return EXIT
        return None


@dataclass
class MeanReversion(Strategy):
    """Buy sharp dips inside an uptrend: close above its long EMA, RSI
    oversold and below the lower Bollinger band. Exit on reversion to the
    mean or after `max_hold` bars. Higher win rate, smaller wins, so it is
    the one most hurt by Robinhood's spread."""

    trend: int = 200
    bb_n: int = 20
    bb_k: float = 2.0
    rsi_n: int = 14
    rsi_buy: float = 30.0
    rsi_exit: float = 55.0
    max_hold: int = 48
    name: str = "meanrev"

    @property
    def warmup(self) -> int:  # type: ignore[override]
        return self.trend + 1

    def prepare(self, candles):
        super().prepare(candles)
        self.et = ind.ema(self.closes, self.trend)
        self.mid = ind.sma(self.closes, self.bb_n)
        self.sd = ind.stdev(self.closes, self.bb_n)
        self.r = ind.rsi(self.closes, self.rsi_n)

    def signal(self, i, in_position, bars_held):
        c = self.closes[i]
        if None in (self.et[i], self.mid[i], self.sd[i], self.r[i]):
            return None
        if not in_position:
            lower = self.mid[i] - self.bb_k * self.sd[i]
            if c > self.et[i] * 0.97 and c < lower and self.r[i] < self.rsi_buy:
                return ENTER
        elif c >= self.mid[i] or self.r[i] > self.rsi_exit or bars_held >= self.max_hold:
            return EXIT
        return None


@dataclass
class TSMomentum(Strategy):
    """Time-series momentum ensemble, the most consistently documented edge
    in crypto: vote across several lookbacks (is price above where it was N
    bars ago?) and hold while enough of them agree. Averaging lookbacks
    avoids betting everything on one tuned window, and the gap between the
    entry and exit thresholds (hysteresis) stops it flipping in and out and
    paying the spread every time the score wobbles."""

    lookbacks: tuple = (20, 60, 120, 240)
    enter_at: float = 0.75   # fraction of lookbacks that must be up to buy
    exit_at: float = 0.25    # sell once only this fraction (or fewer) are up
    name: str = "tsmom"

    @property
    def warmup(self) -> int:  # type: ignore[override]
        return max(self.lookbacks) + 1

    def score(self, i: int) -> float:
        c = self.closes
        return sum(c[i] > c[i - n] for n in self.lookbacks) / len(self.lookbacks)

    def signal(self, i, in_position, bars_held):
        s = self.score(i)
        if not in_position and s >= self.enter_at:
            return ENTER
        if in_position and s <= self.exit_at:
            return EXIT
        return None


class RegimeFilter(Strategy):
    """Wraps a strategy and blocks *entries* unless BTC closes above its
    `period`-bar EMA at that time. Alts fall harder than BTC in bear markets,
    so sitting out when BTC itself is below trend avoids most of the damage.
    Exits are never blocked."""

    def __init__(self, inner: Strategy, btc: list[Candle], period: int):
        self.inner = inner
        closes = [c.close for c in btc]
        e = ind.ema(closes, period)
        self.bull = {c.ts: (e[i] is not None and c.close > e[i]) for i, c in enumerate(btc)}
        self.name = f"{inner.name}+regime"

    @property
    def warmup(self) -> int:  # type: ignore[override]
        return self.inner.warmup

    def prepare(self, candles):
        self.inner.prepare(candles)
        self.candles, self.closes, self.atr = candles, self.inner.closes, self.inner.atr

    def signal(self, i, in_position, bars_held):
        sig = self.inner.signal(i, in_position, bars_held)
        if sig == ENTER and not self.bull.get(self.candles[i].ts, False):
            return None
        return sig


STRATEGIES = {"trend": TrendBreakout, "meanrev": MeanReversion, "tsmom": TSMomentum}


def build(name: str, bar_seconds: int, btc: list[Candle] | None = None,
          regime_days: int = 100) -> Callable[[], Strategy]:
    """Strategy factory with lookbacks expressed in days, so the same
    strategy means the same thing on 1h, 4h or daily bars. With `btc`
    candles, entries are gated by the BTC regime filter.

    Defaults are the middle of the tested range, not the best backtest:
    picking the top of a parameter grid mostly selects luck."""
    per_day = max(1, 86400 // bar_seconds)
    d = lambda days: max(2, int(days * per_day))  # noqa: E731
    if name == "tsmom":
        base = lambda: TSMomentum(lookbacks=(d(7), d(14), d(30), d(60), d(90)),  # noqa: E731
                                  enter_at=0.8, exit_at=0.4)
    elif name == "trend":
        base = lambda: TrendBreakout(entry_n=d(20), exit_n=d(10), fast=d(20), slow=d(50))  # noqa: E731
    elif name == "meanrev":
        base = MeanReversion
    else:
        raise ValueError(f"unknown strategy {name!r}")
    if btc is None:
        return base
    return lambda: RegimeFilter(base(), btc, d(regime_days))


