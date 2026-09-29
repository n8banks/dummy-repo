import pytest

from rhbot import backtest, indicators as ind, screener
from rhbot.broker import PaperBroker
from rhbot.data import Candle, synthetic_universe
from rhbot.risk import CircuitBreaker, RiskConfig, size_position
from rhbot.robinhood import Quote
from rhbot.strategies import ENTER, MeanReversion, Strategy, TrendBreakout
from rhbot.trader import Trader


def test_indicators_basic():
    assert ind.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ind.ema([1.0] * 10, 3)
    assert e[2] == 1.0 and e[-1] == pytest.approx(1.0)
    assert ind.rsi(list(range(1, 30)), 14)[-1] == 100.0
    c = [Candle(i, 10, 11, 9, 10, 1) for i in range(20)]
    assert ind.atr(c, 14)[-1] == pytest.approx(2.0)
    hi, lo = ind.donchian(c, 5)
    assert hi[5] == 11 and lo[5] == 9 and hi[4] is None


def test_sizing_risks_fixed_fraction():
    cfg = RiskConfig(risk_per_trade=0.01, stop_atr_mult=2, max_position_pct=1, max_exposure_pct=1)
    s = size_position(cfg, equity=10_000, cash=10_000, exposure=0, open_positions=0,
                      entry_price=100, atr_value=2, spread_pct=0.0)
    assert s.stop_price == 96
    assert s.quantity * 4 == pytest.approx(100)  # losing 4/unit at stop = 1% of equity


def test_sizing_respects_caps_and_spread_veto():
    cfg = RiskConfig()
    s = size_position(cfg, 10_000, 10_000, 0, 0, 100, 0.01, 0.0)
    assert s.notional <= cfg.max_position_pct * 10_000 + 1e-9
    wide = size_position(cfg, 10_000, 10_000, 0, 0, 100, 0.2, 0.02)
    assert not wide.ok and "spread" in wide.reason
    full = size_position(cfg, 10_000, 10_000, 0, cfg.max_positions, 100, 2, 0.0)
    assert not full.ok


def test_circuit_breaker():
    b = CircuitBreaker(RiskConfig(max_daily_loss=0.03, max_drawdown=0.1))
    b.update(0, 100)
    assert b.can_enter(98)
    assert not b.can_enter(96)
    b.update(10, 89)
    assert b.halted and not b.can_enter(200)


class AlwaysIn(Strategy):
    warmup = 15

    def signal(self, i, in_position, bars_held):
        return None if in_position else ENTER


def flat(n, price=100.0):
    return [Candle(i * 3600, price, price * 1.001, price * 0.999, price, 1000) for i in range(n)]


def test_backtest_charges_spread_on_round_trip():
    res = backtest.run({"X-USD": flat(100)}, AlwaysIn,
                       RiskConfig(max_position_pct=0.5, max_exposure_pct=0.5, stop_atr_mult=50),
                       backtest.CostModel(spread_pct=0.01))
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.ret == pytest.approx(0.995 / 1.005 - 1)  # flat price, lose the spread
    assert res.equity_curve[-1][1] == pytest.approx(10_000 + t.pnl)


def test_backtest_stop_gap_fills_at_open():
    c = flat(40)
    c += [Candle(40 * 3600, 50, 51, 49, 50, 1000)]  # gap far below any stop
    res = backtest.run({"X-USD": c}, AlwaysIn, RiskConfig(max_drawdown=0.99),
                       backtest.CostModel(spread_pct=0.0))
    stop = [t for t in res.trades if t.reason == "stop"]
    assert stop and stop[0].exit == pytest.approx(50)


def test_strategies_and_screen_run_on_synthetic():
    uni = synthetic_universe(["BTC-USD", "ETH-USD", "DOGE-USD"], bars=1500)
    for f in (TrendBreakout, MeanReversion):
        m = backtest.run(uni, f).metrics()
        assert m["final_equity"] > 0 and 0 <= m["max_drawdown"] < 1
    rows = screener.screen(uni, {"DOGE-USD": 0.05}, strategy_factory=TrendBreakout)
    assert {r.symbol for r in rows} == set(uni)
    assert rows[-1].symbol == "DOGE-USD"  # 5% spread gets vetoed


class StubPaper(PaperBroker):
    def __init__(self, tmp, quotes):
        super().__init__(tmp / "book.json", 10_000)
        self.fixed = quotes

    def quotes(self, symbols):
        self._last.update(self.fixed)
        return self.fixed


def test_stop_never_more_than_5pct_below_entry():
    cfg = RiskConfig(stop_atr_mult=10)
    s = size_position(cfg, 10_000, 10_000, 0, 0, 100, atr_value=5, spread_pct=0.004)
    assert s.ok and s.stop_price == pytest.approx(95.0)


def test_budget_grows_daily():
    from rhbot.budget import Budget

    b = Budget(25, "2026-10-02")
    day = 86400
    import datetime as dt

    fri = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc).timestamp()
    assert b.contributed(fri + 10) == 25
    assert b.contributed(fri + 3 * day) == 100
    assert b.contributed(fri - day) == 0


def test_backtest_daily_contributions_are_not_profit():
    res = backtest.run({"X-USD": flat(24 * 5)}, lambda: MeanReversion(),
                       start_equity=0, daily_contribution=25)
    m = res.metrics()
    assert m["contributed"] == 125 and m["final_equity"] == 125
    assert m["profit"] == 0 and m["total_return"] == pytest.approx(0)


def test_trader_paper_cycle_enters_then_stops_out(tmp_path):
    candles = flat(60)
    quotes = {"X-USD": Quote("X-USD", 100, 99.99, 100.01)}
    broker = StubPaper(tmp_path, quotes)
    tr = Trader(broker, ["X-USD"], AlwaysIn, RiskConfig(), "1h", tmp_path / "s.json",
                candle_source=lambda s, i, n: candles)
    now = 61 * 3600
    acts = tr.step(now)
    assert any("BUY" in a for a in acts)
    assert broker.holdings()["X-USD"] > 0
    stop = tr.state["positions"]["X-USD"]["stop"]
    broker.fixed = {"X-USD": Quote("X-USD", stop * 0.99, stop * 0.98, stop)}
    acts = tr.step(now + 3600)
    assert any("SELL" in a and "stop" in a for a in acts)
    assert not broker.holdings()
    assert tr.state["realized_pnl"] < 0 and tr.state["trades"] == 1


def test_trader_respects_budget_and_leaves_user_coins(tmp_path):
    candles = flat(60)
    broker = StubPaper(tmp_path, {"X-USD": Quote("X-USD", 100, 99.99, 100.01)})
    broker.book["holdings"]["X-USD"] = 5.0  # the user's own coins, not the bot's
    from rhbot.budget import Budget

    now = 61 * 3600
    tr = Trader(broker, ["X-USD"], AlwaysIn, RiskConfig(), "1h", tmp_path / "s.json",
                budget=Budget(25, "1970-01-03"), candle_source=lambda s, i, n: candles)
    tr.step(now)
    pos = tr.state["positions"]["X-USD"]
    assert pos["qty"] * pos["entry"] <= 25 * RiskConfig().max_position_pct + 1e-6
    broker.fixed = {"X-USD": Quote("X-USD", 90, 89.99, 90.01)}
    tr.step(now + 3600)
    assert broker.holdings()["X-USD"] == pytest.approx(5.0)  # only the bot's slice was sold
