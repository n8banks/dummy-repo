"""The live/paper loop. Runs once per completed bar:

  1. pull candles (Coinbase) and live quotes (Robinhood)
  2. mark the bot's equity to the bid, update circuit breakers; if halted,
     sell the bot's positions and stop opening new ones
  3. for each bot position: trail the stop, exit on stop hit or strategy exit
  4. for each entry signal: size with risk.py using the *live* spread, buy
  5. persist positions, realized P&L and breaker state to JSON

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

log = logging.getLogger("rhbot")


class Trader:
    def __init__(self, broker: Broker, symbols: list[str], strategy_factory: Callable[[], Strategy],
                 risk: RiskConfig, interval: str = "1h", state_path: str | Path = "state/trader.json",
                 budget: Budget | None = None,
                 candle_source: Callable[[str, str, int], list[data.Candle]] | None = None):
        self.broker = broker
        self.symbols = symbols
        self.factory = strategy_factory
        self.risk = risk
        self.interval = interval
        self.gran = data.GRANULARITIES[interval]
        self.state_path = Path(state_path)
        self.candles = candle_source or (lambda s, i, n: data.fetch_coinbase(s, i, n))
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

    def _save(self) -> None:
        b = self.breaker
        self.state["breaker"] = {"peak_equity": b.peak_equity, "day": b.day,
                                 "day_start_equity": b.day_start_equity,
                                 "halted": b.halted, "halt_reason": b.halt_reason}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def _close(self, sym: str, pos: dict, held_qty: float, price: float, why: str) -> str:
        qty = min(pos["qty"], held_qty)
        self.broker.sell(sym, qty)
        pnl = (price - pos["entry"]) * qty
        self.state["realized_pnl"] += pnl
        self.state["trades"] += 1
        self.state["positions"].pop(sym)
        return f"{sym}: SELL {qty:.8g} @~{price:.6g} ({why}, entry {pos['entry']:.6g}, pnl ${pnl:+.2f})"

    def step(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        actions: list[str] = []
        self.broker.cancel_open_entries()
        quotes = self.broker.quotes(self.symbols)
        held = self.broker.holdings()
        positions = self.state["positions"]
        for sym in list(positions):  # closed outside the bot (or by a native stop)
            if held.get(sym, 0.0) * quotes[sym].bid < 1.0:
                pos = positions.pop(sym)
                # Assume the native stop filled at the stop price.
                self.state["realized_pnl"] += (pos["stop"] - pos["entry"]) * pos["qty"]
                self.state["trades"] += 1
                actions.append(f"{sym}: position gone at broker (native stop?), booked at stop")

        exposure = sum(p["qty"] * quotes[s].bid for s, p in positions.items())
        cost_basis = sum(p["qty"] * p["entry"] for p in positions.values())
        account_cash = self.broker.cash()
        if self.budget:
            bot_cash = self.budget.contributed(now) + self.state["realized_pnl"] - cost_basis
            cash = max(0.0, min(bot_cash, account_cash))
            equity = bot_cash + exposure
        else:
            cash, equity = account_cash, account_cash + exposure
        self.breaker.update(int(now), equity)
        actions.append(f"equity ${equity:,.2f} (cash ${cash:,.2f}, in coins ${exposure:,.2f}, "
                       f"{len(positions)} positions, realized ${self.state['realized_pnl']:+,.2f}, "
                       f"{self.state['trades']} closed trades)")

        if self.breaker.halted:
            for sym, pos in list(positions.items()):
                actions.append(self._close(sym, pos, held.get(sym, 0.0), quotes[sym].bid,
                                           f"HALT: {self.breaker.halt_reason}"))
            self._save()
            return actions

        for sym in self.symbols:
            candles = [c for c in self.candles(sym, self.interval, 400) if c.ts + self.gran <= now]
            strat = self.factory()
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
                actions.append(f"{sym}: BUY {qty:.8g} @{px:.6g} stop {size.stop_price:.6g} "
                               f"(${qty * px:,.2f}, spread {q.spread_pct:.2%})")
            else:
                actions.append(f"{sym}: buy not placed: {res}")

        self._save()
        return actions

    def run_forever(self) -> None:
        while True:
            # wake a few seconds after each bar closes
            nxt = (int(time.time()) // self.gran + 1) * self.gran + 5
            time.sleep(max(1, nxt - time.time()))
            try:
                for a in self.step():
                    log.info(a)
            except Exception:  # keep running; the native stops protect open positions
                log.exception("cycle failed")
