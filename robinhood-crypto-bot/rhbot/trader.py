"""The live/paper loop.

Once per completed bar (`step`): sync with the exchange, update circuit
breakers, then for each symbol trail stops / exit / enter. Every 15 minutes
in between (`check_stops`): sync, breakers, and enforce stops.

Safety rules this module is built around:
  * The bot only acts on orders it created. Every order gets a
    client_order_id that is saved to state *before* the request is sent, so
    after a crash or timeout the outcome can be looked up instead of guessed.
  * Positions are tracked by the bot's own quantity, never by account
    holdings, so coins you hold yourself in the same account are never sold.
  * A resting native stop is how most exits happen. Its fill is discovered by
    polling that order id, and booked at the actual fill price.
  * Moving or removing a stop waits for the cancel to be confirmed; if the
    stop filled first, that fill is booked instead of selling a second time.
    If a replacement can't be placed, the old protection is restored or the
    position is closed.
  * State is saved after every fill.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Callable

from . import data
from .broker import Broker, Fill, OrderUncertain
from .robinhood import RobinhoodError
from .budget import Budget
from .risk import CircuitBreaker, RiskConfig, effective_stop, exit_allowed, size_position
from .strategies import ENTER, EXIT, Strategy
from .util import notify, write_json_atomic

log = logging.getLogger("rhbot")

StrategyBuilder = Callable[[dict[str, list[data.Candle]]], Callable[[], Strategy]]
REGIME_SYMBOL = "BTC-USD"
DUST_USD = 1.0


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
        for k, v in (("positions", {}), ("pending", {}), ("realized_pnl", 0.0), ("trades", 0),
                     ("last_bar", 0)):
            self.state.setdefault(k, v)
        saved = self.state.get("breaker", {})
        fields = CircuitBreaker.__dataclass_fields__
        self.breaker = CircuitBreaker(risk, **{k: v for k, v in saved.items() if k in fields})
        self.budget = budget
        if budget:
            # The start day is fixed on first run so restarts don't reset the allowance.
            budget.start_day = self.state.setdefault(
                "budget_start_day", budget.start_day or dt.datetime.now(dt.timezone.utc).date().isoformat())

    # -- persistence -------------------------------------------------------------

    def _save(self) -> None:
        self.state["breaker"] = self.breaker.to_dict()
        write_json_atomic(self.state_path, self.state)

    def _intent(self, kind: str, sym: str, **extra) -> str:
        """Record an order intent before sending it (write-ahead)."""
        coid = str(uuid.uuid4())
        self.state["pending"][coid] = {"kind": kind, "symbol": sym, "ts": int(time.time()), **extra}
        self._save()
        return coid

    def _done(self, coid: str, save: bool = True) -> None:
        self.state["pending"].pop(coid, None)
        if save:
            self._save()

    def _busy(self, sym: str) -> bool:
        """An order for this symbol is in flight with an unknown outcome. Until
        it's reconciled, placing anything else could double-sell."""
        return any(it["symbol"] == sym for it in self.state["pending"].values())

    # -- bookkeeping -------------------------------------------------------------

    def _book_exit(self, sym: str, fill: Fill, why: str, coid: str | None = None) -> str:
        """Book a (possibly partial) sell fill against the bot's position, clear
        the matching intent, and save once (no crash window in between)."""
        pos = self.state["positions"][sym]
        qty = min(fill.qty, pos["qty"])
        pnl = (fill.price - pos["entry"]) * qty
        self.state["realized_pnl"] += pnl
        pos["qty"] -= qty
        if pos["qty"] * fill.price < DUST_USD:
            self.state["positions"].pop(sym)
            self.state["trades"] += 1
        if coid:
            self._done(coid, save=False)
        self._save()
        msg = f"{sym}: SOLD {qty:.8g} @{fill.price:.6g} ({why}, entry {pos['entry']:.6g}, pnl ${pnl:+.2f})"
        notify(msg, "rhbot sell")
        return msg

    def _book_stop(self, sym: str, pos: dict, f: Fill, why: str) -> str | None:
        """Book only the *new* part of a stop's fill: filled quantity is
        cumulative, and a partly filled stop is seen again next cycle."""
        delta = f.qty - pos.get("stop_booked", 0.0)
        if f.state in ("filled", "canceled", "failed"):
            pos["stop_id"], pos["stop_booked"] = None, 0.0
        else:
            pos["stop_booked"] = f.qty
        if delta <= 0:
            self._save()
            return None
        return self._book_exit(sym, Fill(f.state, delta, f.price, f.order_id), why)

    def _stop_for(self, sym: str, pos: dict) -> float | None:
        return effective_stop(self.risk, sym, pos["entry"], pos["stop"])

    # -- exchange sync -----------------------------------------------------------

    def _sync(self) -> list[str]:
        """Discover native stop fills and resolve interrupted orders."""
        out = []
        for sym, pos in list(self.state["positions"].items()):
            if not pos.get("stop_id"):
                continue
            f = self.broker.order(pos["stop_id"])
            if f.qty > pos.get("stop_booked", 0.0):
                out.append(self._book_stop(sym, pos, f, "native stop"))
            elif f.state in ("canceled", "failed"):
                pos["stop_id"], pos["stop_booked"] = None, 0.0  # removed elsewhere; re-placed later
                self._save()
                out.append(f"{sym}: native stop {f.state} at exchange; will re-place")
        for coid, it in list(self.state["pending"].items()):
            out += self._resolve(coid, it)
        return [m for m in out if m]

    def _resolve(self, coid: str, it: dict) -> list[str | None]:
        sym, kind = it["symbol"], it["kind"]
        f = self.broker.find(coid, sym)
        if f is None:
            if time.time() - it["ts"] > 600:  # never reached the exchange
                self._done(coid)
                return [f"{sym}: {kind} never reached the exchange; cleared"]
            return []
        pos = self.state["positions"].get(sym)
        final = f.state in ("filled", "canceled", "failed")
        if kind == "stop":
            if pos is None:
                if not final:
                    self.broker.cancel(f.order_id)  # orphan: position already gone
                self._done(coid)
                return [f"{sym}: orphan stop cleaned up"]
            if not final:  # it's resting: adopt it instead of placing another
                pos["stop_id"], pos["stop_placed"], pos["stop_booked"] = f.order_id, it["stop"], 0.0
                self._done(coid)
                return [f"{sym}: adopted stop placed during an interrupted request"]
            self._done(coid, save=False)
            if f.qty > 0:
                pos["stop_id"], pos["stop_booked"] = f.order_id, 0.0
                return [self._book_stop(sym, pos, f, "native stop (recovered)")]
            self._save()
            return []
        if kind == "sell":
            if not final:
                return []  # still working; nothing else may be placed for sym meanwhile
            if f.qty > 0 and pos:
                return [self._book_exit(sym, f, "recovered interrupted sell", coid)]
            self._done(coid)
            return []
        # buy
        if not final:
            f = self.broker.cancel(f.order_id)
        if f.qty > 0 and pos is None:
            self.state["positions"][sym] = {"qty": f.qty, "entry": f.price, "stop": it["stop"],
                                            "stop_id": None, "bars": 0, "high": f.price,
                                            "ts": it["ts"]}
            self._done(coid)
            return [f"{sym}: recovered interrupted BUY {f.qty:.8g} @{f.price:.6g}"]
        self._done(coid)
        return []

    def _protect(self, sym: str, pos: dict, bid: float) -> str | None:
        """Make sure the exchange holds the right stop for this position."""
        if self._busy(sym):
            return f"{sym}: waiting on an unresolved order"
        want = self._stop_for(sym, pos)
        msgs = []
        if want is None:
            if pos.get("stop_id"):
                return self._cancel_stop(sym, pos)
            return None
        if want >= bid:
            return self._exit(sym, pos, bid, "stop already crossed")
        if pos.get("stop_id") and abs(pos.get("stop_placed", 0) - want) / want < 0.005:
            return None
        if pos.get("stop_id"):
            m = self._cancel_stop(sym, pos)
            if m:
                msgs.append(m)
            if sym not in self.state["positions"]:
                return "; ".join(msgs)
        qty = self.broker.round_qty(sym, pos["qty"])
        coid = self._intent("stop", sym, stop=want)
        try:
            sid = self.broker.place_stop(sym, qty, want, coid)
        except OrderUncertain:
            return f"{sym}: stop placement uncertain; will reconcile"  # intent stays pending
        if sid is None:
            self._done(coid)
            notify(f"{sym}: could not place stop at {want:.6g}; closing position", "rhbot", "high")
            return "; ".join(msgs + [self._exit(sym, pos, bid, "no stop possible")])
        pos["stop_id"], pos["stop_placed"], pos["stop_booked"] = sid, want, 0.0
        self._done(coid)  # position update and intent removal saved together
        return "; ".join(msgs + [f"{sym}: stop at {want:.6g}"])

    def _cancel_stop(self, sym: str, pos: dict) -> str | None:
        """Cancel the bot's stop and book anything it filled before the cancel
        landed. The position may be reduced or gone afterwards."""
        f = self.broker.cancel(pos["stop_id"])
        return self._book_stop(sym, pos, Fill("canceled" if f.state != "filled" else "filled",
                                              f.qty, f.price, f.order_id),
                               "native stop (filled before cancel)")

    def _exit(self, sym: str, pos: dict, bid: float, why: str) -> str:
        if self._busy(sym):
            return f"{sym}: waiting on an unresolved order"
        msgs = []
        if pos.get("stop_id"):
            m = self._cancel_stop(sym, pos)
            if m:
                msgs.append(m)
            if sym not in self.state["positions"]:
                return "; ".join(msgs)
        qty = self.broker.round_qty(sym, min(pos["qty"], self.broker.available(sym)))
        if qty <= 0:
            notify(f"{sym}: bot position not found at broker; needs a human look", "rhbot", "high")
            return "; ".join(msgs + [f"{sym}: nothing available to sell; left in state for review"])
        coid = self._intent("sell", sym)
        try:
            f = self.broker.sell(sym, qty, coid)
        except OrderUncertain:
            return "; ".join(msgs + [f"{sym}: sell outcome uncertain; will reconcile"])
        except RobinhoodError as e:
            if 400 <= e.status < 500:  # rejected outright: nothing was placed
                self._done(coid)
            raise
        if f.qty <= 0:
            self._done(coid)
            notify(f"{sym}: SELL did not fill ({f.state}); re-protecting", "rhbot", "high")
            self._protect(sym, pos, bid)
            return "; ".join(msgs + [f"{sym}: sell {f.state}; stop re-placed"])
        msgs.append(self._book_exit(sym, f, why, coid))
        if sym in self.state["positions"]:  # partial fill: protect the remainder
            m = self._protect(sym, self.state["positions"][sym], bid)
            if m:
                msgs.append(m)
        return "; ".join(msgs)

    # -- money -------------------------------------------------------------------

    def _money(self, now: float, quotes) -> tuple[float, float, float]:
        """(spendable cash, bot equity, exposure) under the budget rules."""
        positions = self.state["positions"]
        exposure = sum(p["qty"] * (quotes[s].bid if s in quotes else p["entry"])
                       for s, p in positions.items())
        account_cash = self.broker.cash()
        if self.budget:
            cost_basis = sum(p["qty"] * p["entry"] for p in positions.values())
            bot_cash = self.budget.contributed(now) + self.state["realized_pnl"] - cost_basis
            return max(0.0, min(bot_cash, account_cash)), bot_cash + exposure, exposure
        return account_cash, account_cash + exposure, exposure

    def _quotes(self, symbols) -> dict:
        wanted = list(dict.fromkeys(list(symbols) + list(self.state["positions"])))
        q = self.broker.quotes(wanted)
        missing = [s for s in wanted if s not in q]
        if missing:
            log.warning("no quote for %s", missing)
        return q

    def summary(self, now: float | None = None) -> str:
        now = now or time.time()
        quotes = self._quotes(self.symbols)
        cash, equity, exposure = self._money(now, quotes)
        contributed = self.budget.contributed(now) if self.budget else float("nan")
        return (f"equity ${equity:,.2f} (cash ${cash:,.2f}, in coins ${exposure:,.2f}); "
                f"given ${contributed:,.2f}; realized ${self.state['realized_pnl']:+,.2f} over "
                f"{self.state['trades']} trades; open: {', '.join(self.state['positions']) or 'none'}")

    def _halt_if_needed(self, now, quotes, equity) -> list[str] | None:
        self.breaker.update(int(now), equity,
                            self.budget.contributed(now) if self.budget else None)
        if not self.breaker.halted:
            return None
        out = []
        for sym, pos in list(self.state["positions"].items()):
            if sym in quotes:
                try:
                    out.append(self._exit(sym, pos, quotes[sym].bid, f"HALT: {self.breaker.halt_reason}"))
                except Exception as e:
                    log.exception("halt exit failed for %s", sym)
                    out.append(f"{sym}: HALT exit error {e!r}")
        if out:
            notify(f"HALTED: {self.breaker.halt_reason}. Run `trade --reset-halt` to resume.",
                   "rhbot HALT", "high")
        self._save()
        return out

    # -- between bars --------------------------------------------------------------

    def check_stops(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        actions = self._sync()
        if not self.state["positions"]:
            return actions
        quotes = self._quotes([])
        _, equity, _ = self._money(now, quotes)
        halted = self._halt_if_needed(now, quotes, equity)
        if halted is not None:
            return actions + halted
        for sym, pos in list(self.state["positions"].items()):
            if sym not in quotes:
                continue
            try:
                stop = self._stop_for(sym, pos)
                if stop is not None and quotes[sym].bid <= stop:
                    actions.append(self._exit(sym, pos, quotes[sym].bid, "stop"))
                else:
                    msg = self._protect(sym, pos, quotes[sym].bid)
                    if msg:
                        actions.append(msg)
            except Exception as e:
                log.exception("stop check failed for %s", sym)
                actions.append(f"{sym}: stop check error {e!r}")
        self._save()
        return actions

    # -- once per bar ----------------------------------------------------------------

    def step(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        actions = self._sync()
        quotes = self._quotes(self.symbols)
        cash, equity, exposure = self._money(now, quotes)
        halted = self._halt_if_needed(now, quotes, equity)
        if halted is not None:
            return actions + halted

        wanted = list(dict.fromkeys(self.symbols + [REGIME_SYMBOL]))
        history = {}
        for sym in wanted:
            bars = self.candles(sym, self.interval, self.history_bars)
            history[sym] = [c for c in bars if c.ts + self.gran <= now]  # completed bars only
        factory = self.builder(history)
        positions = self.state["positions"]

        for sym in self.symbols:
            try:
                msg = self._step_symbol(sym, history[sym], factory, quotes, equity, cash, exposure)
            except Exception as e:  # one bad symbol must not block the others
                log.exception("step failed for %s", sym)
                notify(f"{sym}: step error {e!r}", "rhbot error", "high")
                msg = f"{sym}: error {e!r}"
            if msg:
                actions.append(msg)
            cash, equity, exposure = self._money(now, quotes)

        # positions the strategy no longer covers (e.g. symbol removed from --symbols)
        for sym, pos in list(positions.items()):
            if sym not in self.symbols and sym in quotes:
                m = self._protect(sym, pos, quotes[sym].bid)
                if m:
                    actions.append(m)

        self.state["last_bar"] = int(now) // self.gran
        self._save()
        actions.append(self.summary(now))
        return actions

    def _step_symbol(self, sym, candles, factory, quotes, equity, cash, exposure) -> str | None:
        strat = factory()
        if len(candles) <= strat.warmup or sym not in quotes:
            return None
        strat.prepare(candles)
        i = len(candles) - 1
        q = quotes[sym]
        pos = self.state["positions"].get(sym)
        atr_now = strat.atr[i] or 0.0

        if pos:
            pos["bars"] += 1
            pos["high"] = max(pos["high"], candles[i].high)
            sig = strat.signal(i, True, pos["bars"])
            stop = self._stop_for(sym, pos)
            if stop is not None and q.bid <= stop:
                return self._exit(sym, pos, q.bid, "stop")
            if sig == EXIT and exit_allowed(self.risk, sym, pos["entry"], q.bid):
                return self._exit(sym, pos, q.bid, "signal")
            pos["stop"] = max(pos["stop"], pos["high"] - self.risk.trail_atr_mult * atr_now)
            return self._protect(sym, pos, q.bid)

        if strat.signal(i, False, 0) != ENTER or not self.breaker.can_enter():
            return None
        size = size_position(self.risk, equity, cash, exposure, len(self.state["positions"]),
                             q.ask, atr_now, q.spread_pct)
        if not size.ok:
            return f"{sym}: entry signal skipped ({size.reason})"
        qty = self.broker.round_qty(sym, size.notional / q.ask)
        if not self.broker.min_ok(sym, qty, q.ask):
            return f"{sym}: entry skipped (below exchange minimum)"
        if self._busy(sym):
            return f"{sym}: waiting on an unresolved order"
        coid = self._intent("buy", sym, stop=size.stop_price)
        try:
            f = self.broker.buy(sym, qty, q.ask, coid)
        except OrderUncertain:
            return f"{sym}: buy outcome uncertain; will reconcile"
        except RobinhoodError as e:
            if 400 <= e.status < 500:  # rejected outright: nothing was placed
                self._done(coid)
            raise
        if f.qty <= 0:
            self._done(coid)
            return f"{sym}: buy not filled ({f.state})"
        pos = {"qty": f.qty, "entry": f.price, "stop": size.stop_price, "stop_id": None,
               "bars": 0, "high": f.price, "ts": int(time.time())}
        self.state["positions"][sym] = pos
        self._done(coid)  # saves the new position
        msg = (f"{sym}: BUY {f.qty:.8g} @{f.price:.6g} stop {size.stop_price:.6g} "
               f"(${f.qty * f.price:,.2f}, spread {q.spread_pct:.2%})")
        notify(msg, "rhbot buy")
        protect = self._protect(sym, pos, q.bid)
        return msg + (f"; {protect}" if protect else "")

    # -- scheduler -----------------------------------------------------------------------

    def run_forever(self, check_every: int = 900) -> None:
        notify(f"started: {self.summary()}", "rhbot")
        while True:
            try:
                now = time.time()
                if int(now) // self.gran > self.state["last_bar"]:
                    try:
                        acts = self.step(now)  # retried every cycle until it succeeds
                        if self.gran >= 86400:
                            notify(acts[-1], "rhbot daily summary", "low")
                    except Exception:
                        log.exception("step failed; running stop checks instead")
                        acts = self.check_stops(now)
                else:
                    acts = self.check_stops(now)
                for a in acts:
                    log.info(a)
            except Exception as e:  # keep running; native stops protect open positions
                log.exception("cycle failed")
                notify(f"cycle failed: {e!r}", "rhbot error", "high")
            time.sleep(check_every - time.time() % check_every + 5)
