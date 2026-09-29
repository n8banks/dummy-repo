"""Long-only signal generators. Robinhood crypto is spot-only (no shorting,
no leverage), so every strategy here only decides when to be long and when
to be flat.

A strategy precomputes its indicators once over the whole candle list, then
`signal(i)` looks only at bars 0..i. The caller acts on the *next* bar, so
nothing here can see the future.
"""

from __future__ import annotations

from dataclasses import dataclass

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


STRATEGIES = {"trend": TrendBreakout, "meanrev": MeanReversion}


def make(name: str, **params) -> Strategy:
    return STRATEGIES[name](**params)
