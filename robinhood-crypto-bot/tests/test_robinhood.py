import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from rhbot.robinhood import RobinhoodClient, Signer, build_order_body, generate_keypair


def test_signature_verifies_over_documented_message():
    priv, pub = generate_keypair()
    s = Signer("key-123", priv)
    body = json.dumps({"a": 1}, separators=(",", ":"))
    h = s.headers("post", "/api/v1/crypto/trading/orders/", body, timestamp=1700000000)
    assert h["x-api-key"] == "key-123"
    assert h["x-timestamp"] == "1700000000"
    msg = f"key-1231700000000/api/v1/crypto/trading/orders/POST{body}".encode()
    Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(
        base64.b64decode(h["x-signature"]), msg)  # raises if wrong


def test_rejects_non_seed_private_key():
    with pytest.raises(ValueError):
        Signer("k", base64.b64encode(b"x" * 64).decode())


def test_order_bodies():
    b = build_order_body("BTC-USD", "buy", "market", asset_quantity="0.001", client_order_id="c1")
    assert b == {"client_order_id": "c1", "side": "buy", "type": "market", "symbol": "BTC-USD",
                 "market_order_config": {"asset_quantity": "0.001"}}
    b = build_order_body("ETH-USD", "sell", "stop_loss", asset_quantity="1", stop_price="2000")
    assert b["stop_loss_order_config"] == {"asset_quantity": "1", "stop_price": "2000",
                                           "time_in_force": "gtc"}
    with pytest.raises(ValueError):
        build_order_body("BTC-USD", "buy", "market", quote_amount="10")
    with pytest.raises(ValueError):
        build_order_body("BTC-USD", "buy", "limit", asset_quantity="1")


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, headers, data, timeout):
        self.calls.append((method, url, headers, data))
        return self.responses.pop(0)


def test_client_signs_query_string_and_parses_quotes():
    priv, _ = generate_keypair()
    sess = FakeSession([FakeResp(200, {"results": [{
        "symbol": "BTC-USD", "price": "100", "bid_inclusive_of_sell_spread": "99.5",
        "ask_inclusive_of_buy_spread": "100.5"}]})])
    c = RobinhoodClient("k", priv, session=sess)
    q = c.quotes(["BTC-USD"])["BTC-USD"]
    assert q.spread_pct == pytest.approx(0.01)
    method, url, headers, data = sess.calls[0]
    assert url.endswith("/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD")
    assert data is None


def test_client_body_sent_is_body_signed():
    priv, pub = generate_keypair()
    sess = FakeSession([FakeResp(201, {"id": "o1", "state": "open"})])
    c = RobinhoodClient("k", priv, session=sess)
    c.place_order("BTC-USD", "buy", "limit", asset_quantity="0.1", limit_price="100",
                  client_order_id="c1")
    _, url, h, data = sess.calls[0]
    msg = f"k{h['x-timestamp']}/api/v1/crypto/trading/orders/POST{data}".encode()
    Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(
        base64.b64decode(h["x-signature"]), msg)
