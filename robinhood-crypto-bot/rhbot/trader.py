"""The live/paper loop.

Once per completed bar (`step`):
  1. pull candles (Coinbase) and live quotes (Robinhood)
  2. mark the bot's equity to the bid, update circuit breakers; if halted,
     sell the bot's positions and stop opening new ones
  3. for each bot position: trail the stop, exit on stop hit or strategy exit
  4. for each entry signal: size with risk.py using the *live* spread, buy
  5. persist positions, realized P&L and breaker state to JSON

Every `check_every` seconds in between (`check_stops`): compare quotes to each
position's stop and sell if hit, and put back any native stop order that's
missing. On daily bars this is what keeps the 5% loss cap tight in paper mode
(live positions also have a native stop resting at Robinhood).

The bot only ever trades its own positions and its own budget (budget.py).
Coins you hold yourself in the same account are never sold, and account cash
beyond the bot's allowance is never spent.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Callable

from . import data
from .broker import Broker
from .budget import Budget
from .risk import CircuitBreaker, RiskConfig, size_position
from .strategies import ENTER, EXIT, Strategy
from .util import notify, write_json_atomic

log = logging.getLogger("rhbot")

StrategyBuilder = Callable[[dict[str, list[data.Candle]]], Callable[[], Strategy]]
REGIME_SYMBOL = "BTC-USD"


def fetch_candles(symbol: str, interval: str, bars: int) -> list[data.Candle]:
    if interval in data.RESAMPLED:
        secs = data.RESAMPLED[interval]
        hourly = data.fetch_coinbase(symbol, "1h", bars * secs // 3600 + 24)
        return data.resample(hourly, secs)
    return data.fetch_coinbase(symbol, interval, bars)


def interval_seconds(interval: str) -> int:
    return data.GRANULARITIES.get(interval) or data.RESAMPLED[interval]


class Trader:
    def __init__(self, broker: Broker, symbols: list[str], strategy_builder: StrategyBuilder,
                 risk: RiskConfig, interval: str = "1d", state_path: str | Path = "state/trader.json",
                 budget: Budget | None = None,
                 candle_source: Callable[[str, str, int], list[data.Candle]] | None = None,
                 history_bars: int = 400):
        self.broker = broker
        self.symbols = symbols
        self.builder = strategy_builder
        self.risk = risk
        self.interval = interval
        self.gran = interval_seconds(interval)
        self.history_bars = history_bars
        self.state_path = Path(state_path)
        self.candles = candle_source or fetch_candles
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.state.setdefault("positions", {})
        self.state.setdefault("realized_pnl", 0.0)
        self.state.setdefault("trades", 0)
        self.breaker = CircuitBreaker(risk, **self.state.get("breaker", {}))
        self.budget = budget
        if budget:
            # The start day is fixed on first run so restarts don't reset the allowance.
            budget.start_day = self.state.setdefault(
                "budget_start_day", budget.start_day or dt.datetime.now(dt.timezone.utc).date().isoformat())

    # -- bookkeeping ---------------------------------------------------------

    def _save(self) -> None:
        b = self.breaker
        self.state["breaker"] = {"peak_equity": b.peak_equity, "day": b.day,
                                 "day_start_equity": b.day_start_equity,
                                 "halted": b.halted, "halt_reason": b.halt_reason}
        write_json_atomic(self.state_path, self.state)

    def _close(self, sym: str, pos: dict, held_qty: float, price: float, why: str) -> str:
        qty = min(pos["qty"], held_qty)
        self.broker.sell(sym, qty)
        pnl = (price - pos["entry"]) * qty
        self.state["realized_pnl"] += pnl
        self.state["trades"] += 1
        self.state["positions"].pop(sym)
        msg = f"{sym}: SELL {qty:.8g} @~{price:.6g} ({why}, entry {pos['entry']:.6g}, pnl ${pnl:+.2f})"
        notify(msg, "rhbot sell")
        return msg

    def _reconcile(self, quotes, held) -> list[str]:
        """Drop positions the broker no longer holds (e.g. a native stop filled)."""
        out = []
        for sym in list(self.state["positions"]):
            if held.get(sym, 0.0) * quotes[sym].bid < 1.0:
                pos = self.state["positions"].pop(sym)
                # Assume the native stop filled at the stop price.
                self.state["realized_pnl"] += (pos["stop"] - pos["entry"]) * pos["qty"]
                self.state["trades"] += 1
                out.append(f"{sym}: position gone at broker (native stop?), booked at stop")
        return out

    def _money(self, now: float, quotes) -> tuple[float, float, float]:
        """(spendable cash, bot equity, exposure) under the budget rules."""
        positions = self.state["positions"]
        exposure = sum(p["qty"] * quotes[s].bid for s, p in positions.items())
        account_cash = self.broker.cash()
        if self.budget:
            cost_basis = sum(p["qty"] * p["entry"] for p in positions.values())
            bot_cash = self.budget.contributed(now) + self.state["realized_pnl"] - cost_basis
            return max(0.0, min(bot_cash, account_cash)), bot_cash + exposure, exposure
        return account_cash, account_cash + exposure, exposure

    def summary(self, now: float | None = None) -> str:
        now = now or time.time()
        quotes = self.broker.quotes(self.symbols)
        cash, equity, exposure = self._money(now, quotes)
        contributed = self.budget.contributed(now) if self.budget else float("nan")
        return (f"equity ${equity:,.2f} (cash ${cash:,.2f}, in coins ${exposure:,.2f}); "
                f"given ${contributed:,.2f}; realized ${self.state['realized_pnl']:+,.2f} over "
                f"{self.state['trades']} trades; open: {', '.join(self.state['positions']) or 'none'}")

    # -- between bars ----------------------------------------------------------

    def check_stops(self, now: float | None = None) -> list[str]:
        positions = self.state["positions"]
        if not positions:
            return []
        quotes = self.broker.quotes(list(positions))
        held = self.broker.holdings()
        actions = self._reconcile(quotes, held)
        for sym, pos in list(positions.items()):
            if quotes[sym].bid <= pos["stop"]:
                actions.append(self._close(sym, pos, held.get(sym, 0.0), quotes[sym].bid, "stop"))
            elif not self.broker.has_stop(sym):
                self.broker.set_stop(sym, pos["qty"], pos["stop"])
                actions.append(f"{sym}: re-placed missing stop at {pos['stop']:.6g}")
        if actions:
            self._save()
        return actions

    # -- once per bar ------------------------------------------------------------

    def step(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        actions: list[str] = []
        self.broker.cancel_open_entries()
        quotes = self.broker.quotes(self.symbols)
        held = self.broker.holdings()
        positions = self.state["positions"]
        actions += self._reconcile(quotes, held)

        cash, equity, exposure = self._money(now, quotes)
        self.breaker.update(int(now), equity)

        if self.breaker.halted:
            for sym, pos in list(positions.items()):
                actions.append(self._close(sym, pos, held.get(sym, 0.0), quotes[sym].bid,
                                           f"HALT: {self.breaker.halt_reason}"))
            if actions:
                notify(f"HALTED: {self.breaker.halt_reason}. Run `trade --reset-halt` to resume.",
                       "rhbot HALT", "high")
            self._save()
            return actions

        wanted = list(dict.fromkeys(self.symbols + [REGIME_SYMBOL]))
        history = {}
        for sym in wanted:
            bars = self.candles(sym, self.interval, self.history_bars)
            history[sym] = [c for c in bars if c.ts + self.gran <= now]  # completed bars only
        factory = self.builder(history)

        for sym in self.symbols:
            candles = history[sym]
            strat = factory()
            if len(candles) <= strat.warmup:
                continue
            strat.prepare(candles)
            i = len(candles) - 1
            q = quotes[sym]
            pos = positions.get(sym)
            atr_now = strat.atr[i] or 0.0

            if pos:
                pos["bars"] += 1
                pos["high"] = max(pos["high"], candles[i].high)
                new_stop = max(pos["stop"], pos["high"] - self.risk.trail_atr_mult * atr_now)
                sig = strat.signal(i, True, pos["bars"])
                if q.bid <= pos["stop"] or sig == EXIT:
                    why = "stop" if q.bid <= pos["stop"] else "signal"
                    actions.append(self._close(sym, pos, held.get(sym, 0.0), q.bid, why))
                    exposure -= pos["qty"] * q.bid
                    cash += pos["qty"] * q.bid
                    continue
                if new_stop > pos["stop"] * 1.005:
                    pos["stop"] = new_stop
                    self.broker.set_stop(sym, pos["qty"], new_stop)
                    actions.append(f"{sym}: trail stop -> {new_stop:.6g}")
                continue

            if strat.signal(i, False, 0) != ENTER or not self.breaker.can_enter(equity):
                continue
            size = size_position(self.risk, equity, cash, exposure, len(positions),
                                 q.ask, atr_now, q.spread_pct)
            if not size.ok:
                actions.append(f"{sym}: entry signal skipped ({size.reason})")
                continue
            res = self.broker.buy(sym, size.notional / q.ask, q.ask)
            if res.get("state") == "filled":
                qty, px = res["qty"], res["price"]
                positions[sym] = {"qty": qty, "entry": px, "stop": size.stop_price, "bars": 0,
                                  "high": px, "ts": int(now)}
                cash -= qty * px
                exposure += qty * px
                self.broker.set_stop(sym, qty, size.stop_price)
                msg = (f"{sym}: BUY {qty:.8g} @{px:.6g} stop {size.stop_price:.6g} "
                       f"(${qty * px:,.2f}, spread {q.spread_pct:.2%})")
                notify(msg, "rhbot buy")
                actions.append(msg)
            else:
                actions.append(f"{sym}: buy not placed: {res}")

        self._save()
        actions.append(self.summary(now))
        return actions

    def run_forever(self, check_every: int = 900) -> None:
        last_bar = int(time.time()) // self.gran
        notify(f"started: {self.summary()}", "rhbot")
        while True:
            time.sleep(check_every - time.time() % check_every + 5)
            try:
                bar = int(time.time()) // self.gran
                if bar != last_bar:
                    last_bar = bar
                    acts = self.step()
                    if self.gran >= 86400:
                        notify(acts[-1], "rhbot daily summary", "low")
                else:
                    acts = self.check_stops()
                for a in acts:
                    log.info(a)
            except Exception as e:  # keep running; the native stops protect open positions
                log.exception("cycle failed")
                notify(f"cycle failed: {e!r}", "rhbot error", "high")
