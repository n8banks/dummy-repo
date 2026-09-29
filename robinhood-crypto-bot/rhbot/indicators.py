"""Plain-Python indicators over lists. Each returns a list aligned with its
input, with None where there isn't enough history yet."""

from __future__ import annotations

import math

from .data import Candle


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    cur = sum(values[:period]) / period
    out[period - 1] = cur
    for i in range(period, len(values)):
        cur = values[i] * k + cur * (1 - k)
        out[i] = cur
    return out


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def stdev(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1 : i + 1]
        m = sum(w) / period
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / period)
    return out


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    """Wilder's average true range."""
    out: list[float | None] = [None] * len(candles)
    trs = []
    for i, c in enumerate(candles):
        prev = candles[i - 1].close if i else c.close
        trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    if len(trs) < period:
        return out
    cur = sum(trs[:period]) / period
    out[period - 1] = cur
    for i in range(period, len(trs)):
        cur = (cur * (period - 1) + trs[i]) / period
        out[i] = cur
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    out[period] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        out[i] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


def donchian(candles: list[Candle], period: int) -> tuple[list[float | None], list[float | None]]:
    """Highest high / lowest low of the *previous* `period` bars (excludes the
    current bar, so a close above it is a genuine breakout)."""
    hi: list[float | None] = [None] * len(candles)
    lo: list[float | None] = [None] * len(candles)
    for i in range(period, len(candles)):
        w = candles[i - period : i]
        hi[i] = max(c.high for c in w)
        lo[i] = min(c.low for c in w)
    return hi, lo


def log_returns(closes: list[float]) -> list[float]:
    return [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]


def realized_vol(closes: list[float], period: int) -> float:
    """Stdev of the last `period` log returns (per bar, not annualized)."""
    r = log_returns(closes[-(period + 1):])
    if len(r) < 2:
        return 0.0
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))


def correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    if n < 3:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va and vb else 0.0
