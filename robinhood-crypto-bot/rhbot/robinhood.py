"""Client for the official Robinhood Crypto Trading API (trading.robinhood.com).

Auth: every request carries three headers.
  x-api-key    issued by Robinhood when you register your public key
  x-timestamp  unix seconds; Robinhood rejects anything older than ~30s
  x-signature  base64 Ed25519 signature over
               api_key + timestamp + path(with query) + METHOD + body
The private key is a base64-encoded 32-byte Ed25519 seed.

Rate limit: 100 requests/minute per account, bursting to 300. A token bucket
below keeps us under it.

Robinhood's API has no historical candles. Price history comes from a
separate source (see data.py); this client only handles quotes, the account,
holdings and orders.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE_URL = "https://trading.robinhood.com"

ACCOUNTS = "/api/v1/crypto/trading/accounts/"
TRADING_PAIRS = "/api/v1/crypto/trading/trading_pairs/"
HOLDINGS = "/api/v1/crypto/trading/holdings/"
ORDERS = "/api/v1/crypto/trading/orders/"
BEST_BID_ASK = "/api/v1/crypto/marketdata/best_bid_ask/"
ESTIMATED_PRICE = "/api/v1/crypto/marketdata/estimated_price/"


class RobinhoodError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Robinhood API {status}: {body[:500]}")
        self.status = status
        self.body = body


def generate_keypair() -> tuple[str, str]:
    """Return (private_seed_b64, public_key_b64). Register the public key in
    Robinhood's API credentials page; keep the private seed secret."""
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(seed).decode(), base64.b64encode(pub).decode()


class Signer:
    def __init__(self, api_key: str, private_seed_b64: str):
        seed = base64.b64decode(private_seed_b64)
        if len(seed) != 32:
            raise ValueError(
                f"Private key must be a base64 32-byte Ed25519 seed, got {len(seed)} bytes"
            )
        self.api_key = api_key
        self._key = Ed25519PrivateKey.from_private_bytes(seed)

    def headers(self, method: str, path: str, body: str, timestamp: int | None = None) -> dict:
        ts = str(int(time.time()) if timestamp is None else timestamp)
        message = f"{self.api_key}{ts}{path}{method.upper()}{body}"
        sig = self._key.sign(message.encode("utf-8"))
        return {
            "x-api-key": self.api_key,
            "x-timestamp": ts,
            "x-signature": base64.b64encode(sig).decode(),
            "Content-Type": "application/json; charset=utf-8",
        }


class TokenBucket:
    """100 req/min refill, 300 burst: Robinhood's documented limits."""

    def __init__(self, capacity: float = 300, per_minute: float = 100):
        self.capacity = capacity
        self.tokens = capacity
        self.rate = per_minute / 60.0
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


@dataclass
class Quote:
    symbol: str
    mid: float
    bid: float  # what you actually receive selling, spread included
    ask: float  # what you actually pay buying, spread included

    @property
    def spread_pct(self) -> float:
        """Round-trip cost as a fraction of mid: buy at ask, sell at bid."""
        return (self.ask - self.bid) / self.mid if self.mid else float("inf")


class RobinhoodClient:
    def __init__(
        self,
        api_key: str,
        private_seed_b64: str,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ):
        self.signer = Signer(api_key, private_seed_b64)
        self.base_url = base_url.rstrip("/")
        self.http = session or requests.Session()
        self.timeout = timeout
        self.bucket = TokenBucket()

    # -- transport -------------------------------------------------------

    def request(self, method: str, path: str, params: Iterable[tuple[str, Any]] | None = None,
                body: dict | None = None) -> Any:
        if params:
            path = f"{path}?{urlencode(list(params))}"
        # Serialize once: the signed bytes must be exactly the sent bytes.
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        # Only reads are retried. A POST that timed out or got a 5xx may still
        # have been executed, so retrying could place the same order twice; the
        # caller reconciles by client_order_id instead (see broker.find).
        attempts = 4 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            self.bucket.acquire()
            headers = self.signer.headers(method, path, payload)
            try:
                resp = self.http.request(
                    method, self.base_url + path, headers=headers,
                    data=payload or None, timeout=self.timeout,
                )
            except requests.RequestException as e:
                if attempt + 1 == attempts:
                    raise RobinhoodError(0, f"network error: {e!r}") from e
                time.sleep(2 ** attempt)
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise RobinhoodError(resp.status_code, resp.text)
            return resp.json() if resp.text else None
        raise AssertionError("unreachable")

    def _paginate(self, path: str, params: list[tuple[str, Any]] | None = None,
                  max_pages: int = 50) -> list[dict]:
        out: list[dict] = []
        page = self.request("GET", path, params)
        for _ in range(max_pages):
            out.extend(page.get("results", []))
            nxt = page.get("next")
            if not nxt:
                break
            # `next` is an absolute URL; re-sign using its path + query.
            u = urlsplit(nxt)
            page = self.request("GET", u.path + (f"?{u.query}" if u.query else ""))
        return out

    # -- account / market data --------------------------------------------

    def account(self) -> dict:
        return self.request("GET", ACCOUNTS)

    def trading_pairs(self, symbols: Iterable[str] = ()) -> list[dict]:
        return self._paginate(TRADING_PAIRS, [("symbol", s) for s in symbols])

    def holdings(self, asset_codes: Iterable[str] = ()) -> list[dict]:
        return self._paginate(HOLDINGS, [("asset_code", a) for a in asset_codes])

    def quotes(self, symbols: Iterable[str]) -> dict[str, Quote]:
        data = self.request("GET", BEST_BID_ASK, [("symbol", s) for s in symbols])
        out = {}
        for r in data.get("results", []):
            out[r["symbol"]] = Quote(
                symbol=r["symbol"],
                mid=float(r["price"]),
                bid=float(r["bid_inclusive_of_sell_spread"]),
                ask=float(r["ask_inclusive_of_buy_spread"]),
            )
        return out

    def estimated_price(self, symbol: str, side: str, quantities: Iterable[float]) -> list[dict]:
        """side: 'bid', 'ask' or 'both'. Quantities in the base asset."""
        q = ",".join(f"{x:.8f}".rstrip("0").rstrip(".") for x in quantities)
        return self.request("GET", ESTIMATED_PRICE,
                            [("symbol", symbol), ("side", side), ("quantity", q)])["results"]

    # -- orders -----------------------------------------------------------

    def place_order(self, symbol: str, side: str, order_type: str, *,
                    asset_quantity: str | None = None, quote_amount: str | None = None,
                    limit_price: str | None = None, stop_price: str | None = None,
                    time_in_force: str = "gtc", client_order_id: str | None = None) -> dict:
        body = build_order_body(symbol, side, order_type, asset_quantity=asset_quantity,
                                quote_amount=quote_amount, limit_price=limit_price,
                                stop_price=stop_price, time_in_force=time_in_force,
                                client_order_id=client_order_id)
        return self.request("POST", ORDERS, body=body)

    def get_order(self, order_id: str) -> dict:
        return self.request("GET", f"{ORDERS}{order_id}/")

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        params = [("state", "open")] + ([("symbol", symbol)] if symbol else [])
        return self._paginate(ORDERS, params)

    def recent_orders(self, symbol: str | None = None, pages: int = 3) -> list[dict]:
        """Most recent orders (newest first), for matching a client_order_id
        after a request whose outcome is unknown."""
        return self._paginate(ORDERS, [("symbol", symbol)] if symbol else None, max_pages=pages)

    def cancel_order(self, order_id: str) -> Any:
        return self.request("POST", f"{ORDERS}{order_id}/cancel/")


def build_order_body(symbol: str, side: str, order_type: str, *,
                     asset_quantity: str | None = None, quote_amount: str | None = None,
                     limit_price: str | None = None, stop_price: str | None = None,
                     time_in_force: str = "gtc", client_order_id: str | None = None) -> dict:
    """Wire body for POST /orders/. Market orders only accept asset_quantity;
    the others accept exactly one of asset_quantity / quote_amount."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if (asset_quantity is None) == (quote_amount is None):
        raise ValueError("give exactly one of asset_quantity or quote_amount")
    size = {"asset_quantity": asset_quantity} if asset_quantity else {"quote_amount": quote_amount}

    if order_type == "market":
        if quote_amount is not None:
            raise ValueError("Robinhood market orders take asset_quantity, not quote_amount")
        config = {"market_order_config": size}
    elif order_type == "limit":
        if not limit_price:
            raise ValueError("limit order needs limit_price")
        config = {"limit_order_config": {**size, "limit_price": limit_price,
                                         "time_in_force": time_in_force}}
    elif order_type == "stop_loss":
        if not stop_price:
            raise ValueError("stop_loss order needs stop_price")
        config = {"stop_loss_order_config": {**size, "stop_price": stop_price,
                                             "time_in_force": time_in_force}}
    elif order_type == "stop_limit":
        if not (limit_price and stop_price):
            raise ValueError("stop_limit order needs limit_price and stop_price")
        config = {"stop_limit_order_config": {**size, "limit_price": limit_price,
                                              "stop_price": stop_price,
                                              "time_in_force": time_in_force}}
    else:
        raise ValueError(f"unknown order type {order_type!r}")

    return {
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "side": side,
        "type": order_type,
        "symbol": symbol,
        **config,
    }
