"""Paper and live brokers behind one interface, so the trader can't tell the
difference and paper results mean something.

PaperBroker fills instantly at the quote's ask/bid (spread included) and
keeps its book in a JSON file. Quotes come from Robinhood when credentials
are set (real spreads), else Coinbase's ticker plus an assumed spread.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Protocol

import requests

from .data import COINBASE
from .robinhood import Quote, RobinhoodClient


@dataclass
class Holding:
    symbol: str
    qty: float


class Broker(Protocol):
    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def cash(self) -> float: ...
    def holdings(self) -> dict[str, float]: ...
    def buy(self, symbol: str, qty: float, limit: float) -> dict: ...
    def sell(self, symbol: str, qty: float) -> dict: ...
    def set_stop(self, symbol: str, qty: float, stop: float) -> None: ...
    def cancel_stop(self, symbol: str) -> None: ...
    def cancel_open_entries(self) -> None: ...


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
                 rh: RobinhoodClient | None = None, assumed_spread: float = 0.008):
        self.path = Path(state_path)
        self.rh = rh
        self.assumed_spread = assumed_spread
        if self.path.exists():
            self.book = json.loads(self.path.read_text())
        else:
            self.book = {"cash": start_cash, "holdings": {}, "stops": {}, "fills": []}
        self._last: dict[str, Quote] = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.book, indent=2))

    def quotes(self, symbols):
        q = self.rh.quotes(symbols) if self.rh else coinbase_quotes(symbols, self.assumed_spread)
        self._last.update(q)
        return q

    def cash(self):
        return self.book["cash"]

    def holdings(self):
        return {k: v for k, v in self.book["holdings"].items() if v > 0}

    def buy(self, symbol, qty, limit):
        px = self._last[symbol].ask
        if px > limit:
            return {"state": "canceled", "reason": "ask moved above limit"}
        cost = px * qty
        if cost > self.book["cash"]:
            return {"state": "rejected", "reason": "insufficient cash"}
        self.book["cash"] -= cost
        self.book["holdings"][symbol] = self.book["holdings"].get(symbol, 0.0) + qty
        self.book["fills"].append({"side": "buy", "symbol": symbol, "qty": qty, "price": px})
        self._save()
        return {"state": "filled", "qty": qty, "price": px}

    def sell(self, symbol, qty):
        px = self._last[symbol].bid
        qty = min(qty, self.book["holdings"].get(symbol, 0.0))
        self.book["cash"] += px * qty
        self.book["holdings"][symbol] -= qty
        self.book["fills"].append({"side": "sell", "symbol": symbol, "qty": qty, "price": px})
        self._save()
        return {"state": "filled", "price": px}

    def set_stop(self, symbol, qty, stop):
        # Paper stops are enforced by the trader loop each cycle.
        self.book["stops"][symbol] = stop
        self._save()

    def cancel_stop(self, symbol):
        self.book["stops"].pop(symbol, None)
        self._save()

    def cancel_open_entries(self):
        pass


def _round_down(x: float, increment: str) -> str:
    inc = Decimal(increment)
    return str((Decimal(str(x)) / inc).to_integral_value(ROUND_DOWN) * inc)


class RobinhoodBroker:
    """Real orders. Entries are marketable limit orders (ask + small buffer) so a
    fast market can't fill us far from the quote; exits are market orders.
    A native stop_loss order sits on every position so a crash is handled
    even if this process is down."""

    def __init__(self, client: RobinhoodClient, symbols: list[str], entry_buffer: float = 0.002):
        self.rh = client
        self.entry_buffer = entry_buffer
        self.pairs = {p["symbol"]: p for p in client.trading_pairs(symbols)}
        # Adopt stops left from a previous run, else a new stop would be
        # rejected because the old one still locks the quantity.
        self.stop_orders: dict[str, str] = {
            o["symbol"]: o["id"] for o in client.open_orders()
            if o.get("side") == "sell" and o.get("type") == "stop_loss"
        }

    def _qty(self, symbol: str, qty: float) -> str:
        return _round_down(qty, self.pairs[symbol]["asset_increment"])

    def _px(self, symbol: str, px: float) -> str:
        return _round_down(px, self.pairs[symbol]["quote_increment"])

    def quotes(self, symbols):
        return self.rh.quotes(symbols)

    def cash(self):
        return float(self.rh.account()["buying_power"])

    def holdings(self):
        out = {}
        for h in self.rh.holdings():
            qty = float(h["total_quantity"])
            if qty > 0:
                out[f'{h["asset_code"]}-USD'] = qty
        return out

    def buy(self, symbol, qty, limit):
        q = self._qty(symbol, qty)
        if float(q) < float(self.pairs[symbol].get("min_order_size", 0)):
            return {"state": "rejected", "reason": "below min_order_size"}
        o = self.rh.place_order(symbol, "buy", "limit", asset_quantity=q,
                                limit_price=self._px(symbol, limit * (1 + self.entry_buffer)))
        # A marketable limit normally fills at once; give it a few seconds, then
        # cancel whatever is left so no stale entry order can fill later unseen.
        for _ in range(10):
            o = self.rh.get_order(o["id"])
            if o.get("state") in ("filled", "canceled", "failed"):
                break
            time.sleep(1)
        if o.get("state") not in ("filled", "canceled", "failed"):
            self.rh.cancel_order(o["id"])
            o = self.rh.get_order(o["id"])
        filled = float(o.get("filled_asset_quantity") or 0)
        if filled <= 0:
            return {"state": o.get("state", "unknown"), "reason": "not filled"}
        return {"state": "filled", "qty": filled,
                "price": float(o.get("average_price") or limit), "order": o}

    def sell(self, symbol, qty):
        self.cancel_stop(symbol)  # a resting stop locks the quantity
        return self.rh.place_order(symbol, "sell", "market", asset_quantity=self._qty(symbol, qty))

    def set_stop(self, symbol, qty, stop):
        self.cancel_stop(symbol)
        o = self.rh.place_order(symbol, "sell", "stop_loss", asset_quantity=self._qty(symbol, qty),
                                stop_price=self._px(symbol, stop))
        self.stop_orders[symbol] = o["id"]

    def cancel_stop(self, symbol):
        oid = self.stop_orders.pop(symbol, None)
        if oid:
            self.rh.cancel_order(oid)

    def cancel_open_entries(self):
        # Catch-all for entries left open by a crash between place and cancel.
        for o in self.rh.open_orders():
            if o.get("side") == "buy":
                self.rh.cancel_order(o["id"])


def is_dust(qty: float, price: float) -> bool:
    return math.isclose(qty, 0.0) or qty * price < 1.0
