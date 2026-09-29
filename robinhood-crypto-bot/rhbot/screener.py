"""Rank coins by how tradable they are for a spread-paying, long-only,
swing-frequency bot.

What makes a coin worth trading here:
  - vol_to_cost: typical daily move divided by round-trip spread. The single
    most important number. If a coin moves 4%/day and costs 0.6% round trip,
    there's room to profit; at 2%/day and 1.5% there isn't.
  - efficiency: Kaufman efficiency ratio (net move / path length). Higher
    means moves are directional (trend-friendly), lower means choppy noise.
  - liquidity: USD volume. Thin coins have wider real spreads and gap more.
  - btc_corr: most alts are highly correlated to BTC; holding four coins
    with 0.9 correlation is one bet, not four. Reported so the portfolio
    step can prefer diversifiers.
  - oos_sharpe (optional): the strategy's Sharpe on the last third of
    history, after costs, with parameters untouched. A coin the strategy
    can't make money on out-of-sample shouldn't be traded by it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Callable

from . import backtest, indicators as ind
from .data import Candle
from .risk import RiskConfig
from .strategies import Strategy

MAINSTREAM = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD",
    "ADA-USD", "AVAX-USD", "LINK-USD", "LTC-USD", "BCH-USD",
]


@dataclass
class CoinScore:
    symbol: str
    daily_vol: float
    spread: float
    vol_to_cost: float
    efficiency: float
    usd_volume_24h: float
    btc_corr: float
    oos_sharpe: float | None
    score: float = 0.0

    def row(self) -> dict:
        return asdict(self)


def efficiency_ratio(closes: list[float], n: int) -> float:
    w = closes[-(n + 1):]
    path = sum(abs(w[i] - w[i - 1]) for i in range(1, len(w)))
    return abs(w[-1] - w[0]) / path if path else 0.0


def screen(universe: dict[str, list[Candle]], spreads: dict[str, float],
           bars_per_day: int = 24, lookback_days: int = 30,
           strategy_factory: Callable[[], Strategy] | None = None,
           risk: RiskConfig | None = None, default_spread: float = 0.008) -> list[CoinScore]:
    n = bars_per_day * lookback_days
    btc = universe.get("BTC-USD")
    btc_rets = ind.log_returns([c.close for c in btc[-(n + 1):]]) if btc else []

    out = []
    for sym, candles in universe.items():
        closes = [c.close for c in candles]
        if len(closes) < n + 1:
            continue
        daily_vol = ind.realized_vol(closes, n) * math.sqrt(bars_per_day)
        spread = spreads.get(sym, default_spread)
        rets = ind.log_returns(closes[-(n + 1):])
        usd_vol = sum(c.close * c.volume for c in candles[-bars_per_day:])
        oos = None
        if strategy_factory:
            cut = len(candles) * 2 // 3
            warm = strategy_factory().warmup
            test = candles[max(0, cut - warm):]
            res = backtest.run({sym: test}, strategy_factory, risk,
                               backtest.CostModel(spread_pct=spread),
                               bars_per_year=bars_per_day * 365)
            oos = res.metrics().get("sharpe", 0.0)
        out.append(CoinScore(
            symbol=sym,
            daily_vol=daily_vol,
            spread=spread,
            vol_to_cost=daily_vol / spread if spread else float("inf"),
            efficiency=efficiency_ratio(closes, n),
            usd_volume_24h=usd_vol,
            btc_corr=ind.correlation(rets, btc_rets) if btc_rets and sym != "BTC-USD" else 1.0,
            oos_sharpe=oos,
        ))
    _score(out)
    return sorted(out, key=lambda s: s.score, reverse=True)


def _rank(values: list[float]) -> list[float]:
    """0..1 percentile rank, higher value => higher rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    r = [0.0] * len(values)
    for pos, i in enumerate(order):
        r[i] = pos / max(1, len(values) - 1)
    return r


def _score(scores: list[CoinScore]) -> None:
    if not scores:
        return
    cols = {
        "vol_to_cost": (0.40, [s.vol_to_cost for s in scores]),
        "liquidity": (0.20, [math.log1p(s.usd_volume_24h) for s in scores]),
        "efficiency": (0.15, [s.efficiency for s in scores]),
        "diversify": (0.10, [-s.btc_corr for s in scores]),
    }
    if all(s.oos_sharpe is not None for s in scores):
        cols["oos"] = (0.15, [s.oos_sharpe for s in scores])  # type: ignore[misc]
    total_w = sum(w for w, _ in cols.values())
    ranks = {k: _rank(v) for k, (_, v) in cols.items()}
    for i, s in enumerate(scores):
        s.score = sum(cols[k][0] * ranks[k][i] for k in cols) / total_w
        # Hard veto: a coin whose daily move barely covers the spread is never worth trading.
        if s.vol_to_cost < 3:
            s.score *= 0.25
