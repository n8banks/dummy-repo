"""The live path (RobinhoodBroker + Trader) against a fake Robinhood exchange
that behaves the awkward ways the real one can: account-wide holdings, stops
that lock quantity, cancels that settle asynchronously, stops that fill
before a cancel lands, and POSTs that are accepted but whose response is lost.
"""

import itertools

import pytest

from rhbot.broker import RobinhoodBroker
from rhbot.data import Candle
from rhbot.risk import RiskConfig
from rhbot.robinhood import Quote, RobinhoodError
from rhbot.strategies import ENTER, Strategy
from rhbot.trader import Trader


class FakeRH:
    def __init__(self, cash=1000.0, holdings=None):
        self.cash = cash
        self.h = dict(holdings or {})  # asset_code -> qty (account-wide, incl. the user's own)
        self.orders: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.px: dict[str, float] = {}
        self.async_cancel = False
        self.lose_next_post = False     # accept the order but "time out" the response
        self.fail_stops = False
        self.posts = []

    # market data / account
    def trading_pairs(self, symbols):
        return [{"symbol": s, "asset_increment": "0.00000001", "quote_increment": "0.01",
                 "min_order_size": "0.000001"} for s in symbols]

    def quotes(self, symbols):
        return {s: Quote(s, self.px[s], self.px[s] * 0.999, self.px[s] * 1.001)
                for s in symbols if s in self.px}

    def account(self):
        return {"buying_power": str(self.cash)}

    def holdings(self, codes=()):
        return [{"asset_code": a, "total_quantity": str(q),
                 "quantity_available_for_trading": str(q - self._locked(a))}
                for a, q in self.h.items() if not codes or a in codes]

    def _locked(self, code):
        return sum(o["qty"] for o in self.orders.values() if o["state"] == "open"
                   and o["side"] == "sell" and o["symbol"].startswith(code + "-"))

    # orders
    def place_order(self, symbol, side, order_type, asset_quantity=None, limit_price=None,
                    stop_price=None, client_order_id=None, **_):
        code, qty = symbol.split("-")[0], float(asset_quantity)
        if side == "sell" and qty > self.h.get(code, 0) - self._locked(code) + 1e-12:
            raise RobinhoodError(400, "insufficient quantity")
        oid = f"o{next(self.ids)}"
        o = {"id": oid, "client_order_id": client_order_id, "symbol": symbol, "side": side,
             "type": order_type, "qty": qty, "state": "open",
             "stop": float(stop_price) if stop_price else None}
        self.orders[oid] = o
        self.posts.append((side, order_type, symbol, qty))
        if order_type == "stop_loss" and self.fail_stops:
            o["state"] = "failed"
        elif order_type in ("market", "limit"):
            self._fill(o, self.px[symbol] * (1.001 if side == "buy" else 0.999))
        if self.lose_next_post:
            self.lose_next_post = False
            raise RobinhoodError(0, "network error: read timed out")
        return dict(o)

    def _fill(self, o, price):
        code = o["symbol"].split("-")[0]
        sign = 1 if o["side"] == "buy" else -1
        self.h[code] = self.h.get(code, 0) + sign * o["qty"]
        self.cash -= sign * o["qty"] * price
        o.update(state="filled", filled_asset_quantity=str(o["qty"]), average_price=str(price))

    def get_order(self, oid):
        o = self.orders[oid]
        if o.pop("cancel_pending", False):  # async cancel settles on a later read
            o["state"] = "canceled"
        return dict(o)

    def cancel_order(self, oid):
        o = self.orders[oid]
        if o["state"] != "open":
            raise RobinhoodError(400, "order is not cancelable")
        if self.async_cancel:
            o["cancel_pending"] = True
        else:
            o["state"] = "canceled"

    def recent_orders(self, symbol=None, pages=3):
        return [dict(o) for o in reversed(list(self.orders.values()))
                if symbol is None or o["symbol"] == symbol]

    def open_orders(self, symbol=None):
        return [dict(o) for o in self.orders.values() if o["state"] == "open"]

    # market moves
    def move(self, symbol, price):
        self.px[symbol] = price
        for o in self.orders.values():
            if o["state"] == "open" and o["type"] == "stop_loss" and o["symbol"] == symbol \
                    and price * 0.999 <= o["stop"]:
                self._fill(o, price * 0.999)


class BuyOnce(Strategy):
    warmup = 15

    def signal(self, i, in_position, bars_held):
        return None if in_position else ENTER


def candles(n=60, price=100.0):
    # ~2% daily range so ATR-based stops sit a few percent below entry
    return [Candle(i * 86400, price, price * 1.01, price * 0.99, price, 1000) for i in range(n)]


def make(tmp_path, fake, **risk):
    broker = RobinhoodBroker(fake, ["ETH-USD"], poll_seconds=0, wait_seconds=0.05)
    tr = Trader(broker, ["ETH-USD"], lambda u: BuyOnce, RiskConfig(**risk), "1d",
                tmp_path / "s.json", candle_source=lambda s, i, n: candles())
    return tr


NOW = 61 * 86400


def test_native_stop_fill_never_sells_users_own_coins(tmp_path):
    fake = FakeRH(holdings={"ETH": 1.0})  # the user's own ETH
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    tr.step(NOW)
    pos = tr.state["positions"]["ETH-USD"]
    bot_qty = pos["qty"]
    assert fake.h["ETH"] == pytest.approx(1.0 + bot_qty)
    fake.move("ETH-USD", pos["stop"] * 0.99)          # exchange fills the native stop
    acts = tr.check_stops(NOW + 900)
    assert any("native stop" in a for a in acts)
    assert "ETH-USD" not in tr.state["positions"]
    tr.check_stops(NOW + 1800)
    tr.step(NOW + 86400)                             # the strategy may buy back in
    bot_now = tr.state["positions"].get("ETH-USD", {}).get("qty", 0.0)
    assert fake.h["ETH"] == pytest.approx(1.0 + bot_now)  # user's coin untouched
    sells = [p for p in fake.posts if p[0] == "sell" and p[1] == "market"]
    assert sells == []


def test_moving_stop_with_async_cancel_never_leaves_position_naked(tmp_path):
    fake = FakeRH()
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    tr.step(NOW)
    fake.async_cancel = True
    tr.state["positions"]["ETH-USD"]["stop"] *= 1.03  # force a trailing move
    tr.check_stops(NOW + 900)
    pos = tr.state["positions"]["ETH-USD"]
    live = [o for o in fake.orders.values() if o["type"] == "stop_loss" and o["state"] == "open"]
    assert len(live) == 1 and live[0]["id"] == pos["stop_id"]
    assert live[0]["stop"] == pytest.approx(round(pos["stop"], 2), abs=0.01)


def test_stop_that_filled_before_cancel_is_booked_not_sold_twice(tmp_path):
    fake = FakeRH()
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    tr.step(NOW)
    pos = tr.state["positions"]["ETH-USD"]
    fake.move("ETH-USD", pos["stop"] * 0.99)  # stop fills at the exchange...
    first_id = pos["stop_id"]
    tr.step(NOW + 86400)                       # ...and the bot then wants to exit too
    assert fake.orders[first_id]["state"] == "filled"
    assert tr.state["trades"] == 1 and tr.state["realized_pnl"] < 0
    assert [p for p in fake.posts if p[1] == "market" and p[0] == "sell"] == []
    bot_now = tr.state["positions"].get("ETH-USD", {}).get("qty", 0.0)  # re-entry allowed
    assert fake.h["ETH"] == pytest.approx(bot_now)


def test_lost_buy_response_is_recovered_not_duplicated(tmp_path):
    fake = FakeRH()
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    fake.lose_next_post = True
    acts = tr.step(NOW)
    assert any("uncertain" in a for a in acts)
    assert tr.state["pending"]                     # intent survives
    acts = tr.check_stops(NOW + 900)
    assert any("recovered" in a for a in acts)
    tr.check_stops(NOW + 1800)
    buys = [p for p in fake.posts if p[0] == "buy"]
    assert len(buys) == 1
    assert tr.state["positions"]["ETH-USD"]["stop_id"]  # and it got protected
    assert not tr.state["pending"]


def test_failed_stop_placement_closes_position(tmp_path):
    fake = FakeRH()
    fake.px["ETH-USD"] = 100.0
    fake.fail_stops = True                         # 200 OK but state "failed"
    tr = make(tmp_path, fake)
    acts = tr.step(NOW)
    assert "ETH-USD" not in tr.state["positions"]
    assert fake.h["ETH"] == pytest.approx(0)
    assert any("SOLD" in a for a in acts)


def test_state_saved_immediately_after_fill(tmp_path):
    fake = FakeRH()
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)

    boom = RuntimeError("crash after the buy filled")

    def crash(*a, **k):
        raise boom

    tr.broker.place_stop = crash
    with pytest.raises(RuntimeError):
        tr._step_symbol("ETH-USD", candles(), lambda: BuyOnce(), fake.quotes(["ETH-USD"]),
                        1000, 1000, 0)
    import json
    saved = json.loads((tmp_path / "s.json").read_text())
    assert "ETH-USD" in saved["positions"]         # a restart still knows about the coins


def test_post_not_retried_on_server_error():
    import json as _json

    from rhbot.robinhood import RobinhoodClient, generate_keypair

    class Resp:
        def __init__(self, code):
            self.status_code, self.text = code, _json.dumps({"detail": "bad gateway"})

        def json(self):
            return _json.loads(self.text)

    class Sess:
        calls = 0

        def request(self, *a, **k):
            Sess.calls += 1
            return Resp(502)

    c = RobinhoodClient("k", generate_keypair()[0], session=Sess())
    with pytest.raises(RobinhoodError):
        c.place_order("BTC-USD", "buy", "market", asset_quantity="0.1")
    assert Sess.calls == 1


class FlakyRH(FakeRH):
    """Adds: lose the response of one order type, fail GETs, slow market sells."""

    def __init__(self, **k):
        super().__init__(**k)
        self.lose_type = None
        self.get_fail = 0
        self.slow_market = False

    def place_order(self, symbol, side, order_type, **k):
        if self.slow_market and order_type == "market":
            code, qty = symbol.split("-")[0], float(k["asset_quantity"])
            if qty > self.h.get(code, 0) - self._locked(code) + 1e-12:
                raise RobinhoodError(400, "insufficient quantity")
            oid = f"o{next(self.ids)}"
            self.orders[oid] = {"id": oid, "client_order_id": k.get("client_order_id"),
                                "symbol": symbol, "side": side, "type": order_type, "qty": qty,
                                "state": "open", "stop": None}
            self.posts.append((side, order_type, symbol, qty))
            return dict(self.orders[oid])
        if self.lose_type == order_type:
            self.lose_type, self.lose_next_post = None, True
        return super().place_order(symbol, side, order_type, **k)

    def get_order(self, oid):
        if self.get_fail:
            self.get_fail -= 1
            raise RobinhoodError(0, "network error: GET timed out")
        return super().get_order(oid)


def open_stops(fake):
    return [o for o in fake.orders.values() if o["type"] == "stop_loss" and o["state"] == "open"]


@pytest.mark.parametrize("user_eth", [1.0, 0.0])
def test_A_lost_stop_response_is_adopted_not_duplicated(tmp_path, user_eth):
    fake = FlakyRH(holdings={"ETH": user_eth} if user_eth else None)
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    fake.lose_type = "stop_loss"
    tr.step(NOW)
    tr.check_stops(NOW + 900)
    tr.check_stops(NOW + 1800)
    pos = tr.state["positions"]["ETH-USD"]
    assert len(open_stops(fake)) == 1 and open_stops(fake)[0]["id"] == pos["stop_id"]
    fake.move("ETH-USD", 90.0)
    tr.check_stops(NOW + 2700)
    tr.check_stops(NOW + 3600)
    assert fake.h.get("ETH", 0) == pytest.approx(user_eth)  # user's coins intact
    assert tr.state["trades"] == 1 and tr.state["realized_pnl"] < 0


def test_B_failed_read_after_accepted_buy_is_recovered(tmp_path):
    fake = FlakyRH()
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    orig = fake.place_order

    def po(*a, **k):
        o = orig(*a, **k)
        if a[2] == "limit":
            fake.get_fail = 4
        return o

    fake.place_order = po
    tr.step(NOW)
    assert tr.state["pending"]  # not discarded
    tr.check_stops(NOW + 900)
    tr.check_stops(NOW + 1800)
    pos = tr.state["positions"]["ETH-USD"]
    assert pos["qty"] == pytest.approx(fake.h["ETH"]) and pos["stop_id"]
    assert len([p for p in fake.posts if p[0] == "buy"]) == 1


def test_C_slow_sell_is_not_repeated(tmp_path):
    fake = FlakyRH(holdings={"ETH": 1.0})
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    tr.step(NOW)
    tr.state["positions"]["ETH-USD"]["stop"] = 99.95  # force a bot-side exit
    fake.slow_market = True
    tr.check_stops(NOW + 900)
    tr.check_stops(NOW + 1800)
    for o in fake.orders.values():
        if o["type"] == "market" and o["state"] == "open":
            fake._fill(o, 99.9)
    fake.slow_market = False
    tr.check_stops(NOW + 2700)
    assert len([p for p in fake.posts if p[1] == "market"]) == 1
    assert fake.h["ETH"] == pytest.approx(1.0)
    assert "ETH-USD" not in tr.state["positions"] and not open_stops(fake)


def test_D_partial_stop_fill_booked_once(tmp_path):
    fake = FlakyRH()
    fake.px["ETH-USD"] = 100.0
    tr = make(tmp_path, fake)
    tr.step(NOW)
    pos = tr.state["positions"]["ETH-USD"]
    q = pos["qty"]
    o = fake.orders[pos["stop_id"]]
    o.update(state="partially_filled", filled_asset_quantity=str(q / 2), average_price="95")
    fake.h["ETH"] -= q / 2
    for k in range(3):
        tr.check_stops(NOW + 900 * (k + 1))
    assert tr.state["positions"]["ETH-USD"]["qty"] == pytest.approx(q / 2)
    assert tr.state["realized_pnl"] == pytest.approx((95 - pos["entry"]) * q / 2)
