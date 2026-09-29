"""Paper and live brokers behind one interface.

The broker is deliberately thin and stateless about *strategy*: it places
orders, waits for them to reach a final state and reports what actually
filled. The trader owns every order id it creates and never touches orders
it didn't place, so coins and orders you manage yourself in the same
account are left alone.

Every order-placing call takes a `client_order_id` chosen (and saved) by the
trader beforehand. If a request dies mid-flight, the trader can later ask
`find(client_order_id)` whether the order exists instead of guessing, which
is what prevents both lost fills and double orders.

PaperBroker simulates the exchange closely enough that paper trading
exercises the same paths as live: resting stop orders lock the quantity and
fill when the bid touches them, sells can't exceed unlocked holdings, and
holdings are account-wide (you can seed "your own" coins to check they're
never touched).
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Protocol

import requests

from .data import COINBASE
from .robinhood import Quote, RobinhoodClient, RobinhoodError
from .util import write_json_atomic

FINAL = ("filled", "canceled", "failed")


class OrderUncertain(RuntimeError):
    """The order may or may not exist; reconcile with find(client_order_id)."""


@dataclass
class Fill:
    state: str          # final order state
    qty: float          # quantity actually filled (0 if none)
    price: float        # average fill price (0 if none)
    order_id: str = ""


def _fill_from(o: dict) -> Fill:
    qty = float(o.get("filled_asset_quantity") or 0)
    price = float(o.get("average_price") or 0) if qty else 0.0
    return Fill(o.get("state", "unknown"), qty, price, o.get("id", ""))


class Broker(Protocol):
    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def cash(self) -> float: ...
    def available(self, symbol: str) -> float: ...
    def round_qty(self, symbol: str, qty: float) -> float: ...
    def min_ok(self, symbol: str, qty: float, price: float) -> bool: ...
    def buy(self, symbol: str, qty: float, limit: float, coid: str) -> Fill: ...
    def sell(self, symbol: str, qty: float, coid: str) -> Fill: ...
    def place_stop(self, symbol: str, qty: float, stop: float, coid: str) -> str | None: ...
    def cancel(self, order_id: str) -> Fill: ...
    def order(self, order_id: str) -> Fill: ...
    def find(self, coid: str, symbol: str) -> Fill | None: ...


# -- paper -------------------------------------------------------------------


def coinbase_quotes(symbols: list[str], assumed_spread: float,
                    http: requests.Session | None = None) -> dict[str, Quote]:
    http = http or requests.Session()
    out = {}
    for s in symbols:
        r = http.get(f"{COINBASE}/products/{s}/ticker", timeout=10)
        r.raise_for_status()
        mid = float(r.json()["price"])
        out[s] = Quote(s, mid, mid * (1 - assumed_spread / 2), mid * (1 + assumed_spread / 2))
    return out


class PaperBroker:
    def __init__(self, state_path: str | Path, start_cash: float = 10_000.0,
                 rh: RobinhoodClient | None = None, assumed_spread: float = 0.02):
        self.path = Path(state_path)
        self.rh = rh
        self.assumed_spread = assumed_spread
        if self.path.exists():
            self.book = json.loads(self.path.read_text())
        else:
            self.book = {"cash": start_cash, "holdings": {}, "orders": {}, "fills": []}
        self.book.setdefault("orders", {})
        self._ids = itertools.count(len(self.book["orders"]) + 1)
        self._last: dict[str, Quote] = {}

    def _save(self) -> None:
        write_json_atomic(self.path, self.book)

    def _fetch_quotes(self, symbols):
        return self.rh.quotes(symbols) if self.rh else coinbase_quotes(symbols, self.assumed_spread)

    def quotes(self, symbols):
        q = self._fetch_quotes(symbols)
        self._last.update(q)
        self._trigger_stops()
        return q

    def _trigger_stops(self):
        """Resting stops fill at the bid once it touches the stop (like a
        stop-market order: a gap fills below the stop)."""
        changed = False
        for o in self.book["orders"].values():
            q = self._last.get(o["symbol"])
            if o["state"] == "open" and o["type"] == "stop_loss" and q and q.bid <= o["stop"]:
                self._fill(o, q.bid)
                changed = True
        if changed:
            self._save()

    def _locked(self, symbol):
        return sum(o["qty"] for o in self.book["orders"].values()
                   if o["state"] == "open" and o["side"] == "sell" and o["symbol"] == symbol)

    def _fill(self, o, price):
        sym, qty = o["symbol"], o["qty"]
        if o["side"] == "buy":
            self.book["cash"] -= price * qty
            self.book["holdings"][sym] = self.book["holdings"].get(sym, 0.0) + qty
        else:
            self.book["cash"] += price * qty
            self.book["holdings"][sym] = self.book["holdings"].get(sym, 0.0) - qty
        o.update(state="filled", filled_asset_quantity=qty, average_price=price)
        self.book["fills"].append({"side": o["side"], "symbol": sym, "qty": qty, "price": price})

    def _new(self, symbol, side, typ, qty, coid, stop=None):
        oid = f"paper-{next(self._ids)}"
        o = {"id": oid, "client_order_id": coid, "symbol": symbol, "side": side, "type": typ,
             "qty": qty, "stop": stop, "state": "open"}
        self.book["orders"][oid] = o
        return o

    def cash(self):
        return self.book["cash"]

    def available(self, symbol):
        return self.book["holdings"].get(symbol, 0.0) - self._locked(symbol)

    def round_qty(self, symbol, qty):
        return float(Decimal(str(qty)).quantize(Decimal("0.00000001"), ROUND_DOWN))

    def min_ok(self, symbol, qty, price):
        return qty > 0 and qty * price >= 1.0

    def buy(self, symbol, qty, limit, coid):
        o = self._new(symbol, "buy", "limit", qty, coid)
        q = self._last[symbol]
        if q.ask > limit or q.ask * qty > self.book["cash"]:
            o["state"] = "canceled"
        else:
            self._fill(o, q.ask)
        self._save()
        return _fill_from(o)

    def sell(self, symbol, qty, coid):
        if qty > self.available(symbol) + 1e-12:
            raise RobinhoodError(400, f"insufficient quantity for {symbol}")
        o = self._new(symbol, "sell", "market", qty, coid)
        self._fill(o, self._last[symbol].bid)
        self._save()
        return _fill_from(o)

    def place_stop(self, symbol, qty, stop, coid):
        if qty > self.available(symbol) + 1e-12:
            return None
        o = self._new(symbol, "sell", "stop_loss", qty, coid, stop)
        self._save()
        self._trigger_stops()
        return o["id"]

    def cancel(self, order_id):
        o = self.book["orders"][order_id]
        if o["state"] == "open":
            o["state"] = "canceled"
            self._save()
        return _fill_from(o)

    def order(self, order_id):
        return _fill_from(self.book["orders"][order_id])

    def find(self, coid, symbol):
        for o in self.book["orders"].values():
            if o.get("client_order_id") == coid:
                return _fill_from(o)
        return None


# -- live --------------------------------------------------------------------


class RobinhoodBroker:
    """Real orders. Entries are marketable limit orders (ask + a small buffer)
    so a fast market can't fill far from the quote; exits are market orders;
    protective stops are native stop_loss orders resting at Robinhood."""

    def __init__(self, client: RobinhoodClient, symbols: list[str], entry_buffer: float = 0.002,
                 poll_seconds: float = 1.0, wait_seconds: float = 30.0):
        self.rh = client
        self.entry_buffer = entry_buffer
        self.poll = poll_seconds
        self.wait_s = wait_seconds
        self.pairs = {p["symbol"]: p for p in client.trading_pairs(symbols)}

    # -- sizing helpers ---------------------------------------------------

    def _pair(self, symbol):
        if symbol not in self.pairs:
            self.pairs.update({p["symbol"]: p for p in self.rh.trading_pairs([symbol])})
        return self.pairs[symbol]

    def _fmt(self, x: float, increment: str) -> str:
        inc = Decimal(increment)
        d = (Decimal(str(x)) / inc).to_integral_value(ROUND_DOWN) * inc
        return f"{d.normalize():f}"

    def round_qty(self, symbol, qty):
        return float(self._fmt(qty, self._pair(symbol)["asset_increment"]))

    def min_ok(self, symbol, qty, price):
        p = self._pair(symbol)
        if qty <= 0:
            return False
        if p.get("min_order_size") and qty < float(p["min_order_size"]):
            return False
        if p.get("min_order_amount") and qty * price < float(p["min_order_amount"]):
            return False
        return True

    # -- account ---------------------------------------------------------

    def quotes(self, symbols):
        return self.rh.quotes(symbols)

    def cash(self):
        return float(self.rh.account()["buying_power"])

    def available(self, symbol):
        code = symbol.split("-")[0]
        for h in self.rh.holdings([code]):
            if h["asset_code"] == code:
                return float(h.get("quantity_available_for_trading", h["total_quantity"]))
        return 0.0

    # -- orders ------------------------------------------------------------

    def _wait(self, order_id: str, seconds: float | None = None) -> dict:
        deadline = time.monotonic() + (self.wait_s if seconds is None else seconds)
        while True:
            o = self.rh.get_order(order_id)
            if o.get("state") in FINAL or time.monotonic() >= deadline:
                return o
            time.sleep(self.poll)

    def _place(self, coid: str, symbol: str, **kw) -> dict:
        try:
            return self.rh.place_order(symbol, client_order_id=coid, **kw)
        except RobinhoodError as e:
            if e.status == 0 or e.status == 429 or e.status >= 500:
                raise OrderUncertain(str(e)) from e  # may have been accepted
            raise

    def buy(self, symbol, qty, limit, coid):
        pair = self._pair(symbol)
        o = self._place(coid, symbol, side="buy", order_type="limit",
                        asset_quantity=self._fmt(qty, pair["asset_increment"]),
                        limit_price=self._fmt(limit * (1 + self.entry_buffer), pair["quote_increment"]))
        # A marketable limit normally fills at once. Whatever hasn't filled in
        # a few seconds is cancelled, and we wait for the cancel to settle so a
        # late fill can't happen unseen.
        try:
            o = self._wait(o["id"], 10)
            if o.get("state") not in FINAL:
                return self.cancel(o["id"])
            return _fill_from(o)
        except OrderUncertain:
            raise
        except Exception as e:  # the order exists; only its outcome is unknown
            raise OrderUncertain(f"buy {o['id']} placed, outcome unknown: {e!r}") from e

    def sell(self, symbol, qty, coid):
        pair = self._pair(symbol)
        o = self._place(coid, symbol, side="sell", order_type="market",
                        asset_quantity=self._fmt(qty, pair["asset_increment"]))
        try:
            o = self._wait(o["id"])
        except Exception as e:
            raise OrderUncertain(f"sell {o['id']} placed, outcome unknown: {e!r}") from e
        if o.get("state") not in FINAL:
            raise OrderUncertain(f"sell {o['id']} still {o.get('state')} after {self.wait_s}s")
        return _fill_from(o)

    def place_stop(self, symbol, qty, stop, coid):
        pair = self._pair(symbol)
        try:
            o = self._place(coid, symbol, side="sell", order_type="stop_loss",
                            asset_quantity=self._fmt(qty, pair["asset_increment"]),
                            stop_price=self._fmt(stop, pair["quote_increment"]))
        except OrderUncertain:
            raise
        except RobinhoodError:
            return None  # rejected outright
        # A 200 can still carry state "failed": re-read before trusting the stop.
        try:
            o = self.rh.get_order(o["id"])
        except Exception as e:
            raise OrderUncertain(f"stop {o['id']} placed, state unknown: {e!r}") from e
        return o["id"] if o.get("state") in ("open", "pending", "confirmed", "queued") else None

    def cancel(self, order_id):
        """Cancel and wait until the order is final. If it filled before the
        cancel landed, the returned Fill says so - the caller must book it."""
        try:
            self.rh.cancel_order(order_id)
        except RobinhoodError:
            pass  # not cancelable: already filled/canceled; the state below tells us which
        o = self._wait(order_id)
        if o.get("state") not in FINAL:
            raise OrderUncertain(f"cancel of {order_id} not confirmed (state {o.get('state')})")
        return _fill_from(o)

    def order(self, order_id):
        return _fill_from(self.rh.get_order(order_id))

    def find(self, coid, symbol):
        for o in self.rh.recent_orders(symbol):
            if o.get("client_order_id") == coid:
                return _fill_from(o)
        return None
