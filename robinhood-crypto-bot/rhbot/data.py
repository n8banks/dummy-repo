"""Price history. Robinhood's API has quotes but no candles, so history
comes from Coinbase's public (no-key) market data. Robinhood routes to the
same venues, so Coinbase prices track Robinhood's mid closely; the spread
you actually pay is measured live from Robinhood's best_bid_ask.

A synthetic generator is included so tests and demos run offline.
"""

from __future__ import annotations

import csv
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path

import requests

COINBASE = "https://api.exchange.coinbase.com"
GRANULARITIES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
# Intervals Coinbase doesn't serve directly; built by resampling 1h candles.
RESAMPLED = {"4h": 4 * 3600, "12h": 12 * 3600}


@dataclass
class Candle:
    ts: int  # unix seconds, bar open
    open: float
    high: float
    low: float
    close: float
    volume: float


def fetch_coinbase(symbol: str, interval: str = "1h", bars: int = 2000,
                   session: requests.Session | None = None) -> list[Candle]:
    """Fetch up to `bars` candles for e.g. 'BTC-USD', oldest first.
    Coinbase returns at most 300 per call, so this pages backwards."""
    gran = GRANULARITIES[interval]
    http = session or requests.Session()
    end = int(time.time()) // gran * gran
    out: dict[int, Candle] = {}
    while len(out) < bars:
        start = end - 300 * gran
        r = http.get(
            f"{COINBASE}/products/{symbol}/candles",
            params={"granularity": gran, "start": start, "end": end},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for t, lo, hi, op, cl, vol in rows:
            out[int(t)] = Candle(int(t), float(op), float(hi), float(lo), float(cl), float(vol))
        end = start
        time.sleep(0.35)  # public endpoint allows ~3 req/s
    return sorted(out.values(), key=lambda c: c.ts)[-bars:]


def resample(candles: list[Candle], seconds: int) -> list[Candle]:
    """Aggregate candles into `seconds`-long bars aligned to UTC (e.g. 1h -> 4h
    or 1d). A partial trailing bar is dropped."""
    out: list[Candle] = []
    bucket: list[Candle] = []
    for c in candles:
        if bucket and c.ts // seconds != bucket[0].ts // seconds:
            out.append(_merge(bucket, seconds))
            bucket = []
        bucket.append(c)
    step = candles[1].ts - candles[0].ts if len(candles) > 1 else seconds
    if bucket and bucket[-1].ts + step >= (bucket[0].ts // seconds + 1) * seconds:
        out.append(_merge(bucket, seconds))  # last bucket is complete
    return out


def _merge(b: list[Candle], seconds: int) -> Candle:
    return Candle(b[0].ts // seconds * seconds, b[0].open, max(c.high for c in b),
                  min(c.low for c in b), b[-1].close, sum(c.volume for c in b))


def save_csv(candles: list[Candle], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts, c.open, c.high, c.low, c.close, c.volume])


def load_csv(path: str | Path) -> list[Candle]:
    with Path(path).open() as f:
        return [
            Candle(int(r["ts"]), float(r["open"]), float(r["high"]), float(r["low"]),
                   float(r["close"]), float(r["volume"]))
            for r in csv.DictReader(f)
        ]


# Rough hourly vol and BTC beta per coin, used only for synthetic data.
SYNTH_PROFILES = {
    "BTC-USD": (0.006, 1.0, 60000.0),
    "ETH-USD": (0.008, 1.1, 3000.0),
    "SOL-USD": (0.012, 1.3, 150.0),
    "XRP-USD": (0.011, 1.0, 0.6),
    "DOGE-USD": (0.014, 1.2, 0.15),
    "ADA-USD": (0.012, 1.1, 0.45),
    "AVAX-USD": (0.013, 1.3, 30.0),
    "LINK-USD": (0.012, 1.2, 15.0),
    "LTC-USD": (0.010, 0.9, 80.0),
    "BCH-USD": (0.011, 0.9, 400.0),
}


def synthetic_universe(symbols: list[str], bars: int = 3000, interval: str = "1h",
                       seed: int = 7) -> dict[str, list[Candle]]:
    """Correlated random walks with volatility clustering and trending regimes.
    Good for exercising the code; says nothing about real profitability."""
    rng = random.Random(seed)
    gran = GRANULARITIES[interval]
    start = 1_700_000_000 // gran * gran
    # Shared market factor with GARCH(1,1)-style vol and slowly switching drift.
    market, var, drift = [], 1.0, 0.0
    for _ in range(bars):
        if rng.random() < 0.01:
            drift = rng.choice([-0.15, 0.0, 0.0, 0.15])
        shock = rng.gauss(0, 1)
        var = 0.05 + 0.9 * var + 0.05 * shock * shock
        market.append(drift + math.sqrt(var) * shock)

    out = {}
    for sym in symbols:
        vol, beta, price = SYNTH_PROFILES.get(sym, (0.012, 1.1, 10.0))
        candles, ivar = [], 1.0
        for i in range(bars):
            idio = rng.gauss(0, 1)
            ivar = 0.05 + 0.9 * ivar + 0.05 * idio * idio
            z = beta * market[i] + math.sqrt(max(0.0, 1.0 - min(beta, 1.0) ** 2) + 0.3) * math.sqrt(ivar) * idio
            ret = vol * z / 1.4
            op = price
            cl = max(1e-9, price * math.exp(ret))
            wick = abs(rng.gauss(0, vol * 0.5))
            hi = max(op, cl) * (1 + wick)
            lo = min(op, cl) * (1 - abs(rng.gauss(0, vol * 0.5)))
            candles.append(Candle(start + i * gran, op, hi, lo, cl, rng.uniform(50, 150) * 1e6 / cl))
            price = cl
        out[sym] = candles
    return out
