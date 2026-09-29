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
    day = 86400
    b = CircuitBreaker(RiskConfig(max_daily_loss=0.03, max_drawdown=0.1))
    b.update(0, 100)
    b.update(day, 98)
    assert b.can_enter()          # -2% vs a day ago
    b.update(2 * day, 94)
    assert not b.can_enter()      # -4% vs a day ago (checked once a day still works)
    b.update(3 * day, 89)
    assert b.halted and not b.can_enter()
    b.reset()
    assert b.can_enter() or not b.halted


def test_circuit_breaker_ignores_deposits():
    b = CircuitBreaker(RiskConfig(max_drawdown=0.15))
    b.update(0, 100, contributed=100)
    b.update(86400, 190, contributed=200)   # +100 deposit, -10 trading loss
    assert b.index == pytest.approx(0.9)
    b.update(2 * 86400, 270, contributed=300)  # another deposit hides nothing
    assert b.halted


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

    def _fetch_quotes(self, symbols):
        return dict(self.fixed)


def test_stop_never_wider_than_cap():
    cfg = RiskConfig(stop_atr_mult=10)
    s = size_position(cfg, 10_000, 10_000, 0, 0, 100, atr_value=5, spread_pct=0.004)
    assert s.ok and s.stop_price == pytest.approx(85.0)  # default cap 15%
    s = size_position(RiskConfig(stop_atr_mult=10, max_stop_pct=0.05), 10_000, 10_000, 0, 0,
                      100, atr_value=5, spread_pct=0.004)
    assert s.stop_price == pytest.approx(95.0)


def test_wider_stop_means_smaller_position_same_risk():
    kw = dict(risk_per_trade=0.005, max_position_pct=1, max_exposure_pct=1, stop_atr_mult=100)
    tight = size_position(RiskConfig(max_stop_pct=0.05, **kw), 10_000, 10_000, 0, 0, 100, 1, 0.0)
    wide = size_position(RiskConfig(max_stop_pct=0.15, **kw), 10_000, 10_000, 0, 0, 100, 1, 0.0)
    assert wide.notional < tight.notional
    for s in (tight, wide):
        assert s.quantity * (100 - s.stop_price) == pytest.approx(50)  # 0.5% of 10k either way


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
    tr = Trader(broker, ["X-USD"], lambda u: AlwaysIn, RiskConfig(), "1h", tmp_path / "s.json",
                candle_source=lambda s, i, n: candles)
    now = 61 * 3600
    acts = tr.step(now)
    assert any("BUY" in a for a in acts)
    assert broker.book["holdings"]["X-USD"] > 0
    assert tr.state["positions"]["X-USD"]["stop_id"]  # protective stop resting
    stop = tr.state["positions"]["X-USD"]["stop"]
    broker.fixed = {"X-USD": Quote("X-USD", stop * 0.99, stop * 0.98, stop)}
    acts = tr.step(now + 3600)
    assert any("SOLD" in a and "stop" in a for a in acts)
    assert broker.book["holdings"]["X-USD"] == pytest.approx(0)
    assert tr.state["realized_pnl"] < 0 and tr.state["trades"] == 1


def test_trader_respects_budget_and_leaves_user_coins(tmp_path):
    candles = flat(60)
    broker = StubPaper(tmp_path, {"X-USD": Quote("X-USD", 100, 99.99, 100.01)})
    broker.book["holdings"]["X-USD"] = 5.0  # the user's own coins, not the bot's
    from rhbot.budget import Budget

    now = 61 * 3600
    tr = Trader(broker, ["X-USD"], lambda u: AlwaysIn, RiskConfig(), "1h", tmp_path / "s.json",
                budget=Budget(25, "1970-01-03"), candle_source=lambda s, i, n: candles)
    tr.step(now)
    pos = tr.state["positions"]["X-USD"]
    assert pos["qty"] * pos["entry"] <= 25 * RiskConfig().max_position_pct + 1e-6
    broker.fixed = {"X-USD": Quote("X-USD", 90, 89.99, 90.01)}
    tr.step(now + 3600)
    assert broker.book["holdings"]["X-USD"] == pytest.approx(5.0)  # only the bot's slice was sold


def test_budget_cap():
    from rhbot.budget import Budget

    b = Budget(25, "1970-01-01", cap=500)
    assert b.contributed(19 * 86400) == 500
    assert b.contributed(100 * 86400) == 500


def test_resample_hourly_to_daily():
    from rhbot.data import resample

    hours = [Candle(i * 3600, 10 + i, 20 + i, 1 + i, 11 + i, 1) for i in range(50)]
    days = resample(hours, 86400)
    assert len(days) == 2  # hours 48-49 are an incomplete third day
    d = days[1]
    assert (d.ts, d.open, d.close) == (86400, 34, 58)
    assert d.high == 20 + 47 and d.low == 1 + 24 and d.volume == 24


def test_tsmom_hysteresis():
    from rhbot.strategies import EXIT, TSMomentum

    up = [Candle(i, p, p, p, p, 1) for i, p in enumerate(range(100, 130))]
    s = TSMomentum(lookbacks=(2, 5, 10), enter_at=1.0, exit_at=0.0)
    s.prepare(up)
    assert s.signal(len(up) - 1, False, 0) == ENTER
    down = up + [Candle(30 + i, p, p, p, p, 1) for i, p in enumerate(range(128, 110, -1))]
    s.prepare(down)
    assert s.signal(len(down) - 1, True, 5) == EXIT


def test_regime_filter_blocks_entries_only():
    from rhbot.strategies import EXIT, RegimeFilter

    class Flip(Strategy):
        def signal(self, i, in_position, bars_held):
            return EXIT if in_position else ENTER

    btc_down = [Candle(i, 100 - i, 100 - i, 100 - i, 100 - i, 1) for i in range(40)]
    f = RegimeFilter(Flip(), btc_down, 10)
    f.prepare(flat(40))
    assert f.signal(30, False, 0) is None  # BTC below its EMA: no new buys
    assert f.signal(30, True, 3) == EXIT   # but exits still go through


def test_atomic_write_keeps_old_file_on_failure(tmp_path):
    from rhbot.util import write_json_atomic

    p = tmp_path / "s.json"
    write_json_atomic(p, {"a": 1})
    with pytest.raises(TypeError):
        write_json_atomic(p, {"a": object()})
    import json
    assert json.loads(p.read_text()) == {"a": 1}
    assert list(tmp_path.iterdir()) == [p]


def test_check_stops_between_bars(tmp_path):
    candles = flat(60)
    broker = StubPaper(tmp_path, {"X-USD": Quote("X-USD", 100, 99.99, 100.01)})
    tr = Trader(broker, ["X-USD"], lambda u: AlwaysIn, RiskConfig(), "1h", tmp_path / "s.json",
                candle_source=lambda s, i, n: candles)
    tr.step(61 * 3600)
    assert tr.check_stops(61 * 3600 + 900) == []
    stop = tr.state["positions"]["X-USD"]["stop"]
    broker.fixed = {"X-USD": Quote("X-USD", stop, stop * 0.999, stop * 1.001)}
    acts = tr.check_stops(61 * 3600 + 1800)
    assert any("SOLD" in a for a in acts) and not tr.state["positions"]


def test_build_strategies_scale_with_interval():
    from rhbot.strategies import build

    daily = build("tsmom", 86400)()
    hourly = build("tsmom", 3600)()
    assert hourly.warmup - 1 == 24 * (daily.warmup - 1)
    btc = flat(200)
    assert build("trend", 86400, btc)().name == "trend+regime"


def test_cash_apy_accrues_on_idle_cash():
    res = backtest.run({"X-USD": flat(24 * 366)}, lambda: MeanReversion(), start_equity=1000,
                       cash_apy=0.05)
    assert res.equity_curve[-1][1] == pytest.approx(1050, rel=2e-3)


def test_hold_to_profit_rules():
    from rhbot.risk import effective_stop, exit_allowed

    cfg = RiskConfig(hold_to_profit=("BTC-USD",), min_exit_profit=0.01)
    assert effective_stop(cfg, "ETH-USD", 100, 90) == 90          # normal coin: normal stop
    assert effective_stop(cfg, "BTC-USD", 100, 90) is None        # held coin below profit: no stop
    assert effective_stop(cfg, "BTC-USD", 100, 105) == 105        # trailing stop locks a profit
    assert effective_stop(RiskConfig(hold_to_profit=("BTC-USD",), hold_floor=0.4),
                          "BTC-USD", 100, 90) == pytest.approx(60)  # disaster floor
    assert not exit_allowed(cfg, "BTC-USD", 100, 100.5)
    assert exit_allowed(cfg, "BTC-USD", 100, 101.5)
    assert exit_allowed(cfg, "ETH-USD", 100, 50)


def test_trader_hold_to_profit_does_not_sell_at_a_loss(tmp_path):
    candles = flat(60)
    broker = StubPaper(tmp_path, {"X-USD": Quote("X-USD", 100, 99.99, 100.01)})
    tr = Trader(broker, ["X-USD"], lambda u: AlwaysIn, RiskConfig(hold_to_profit=("X-USD",)),
                "1h", tmp_path / "s.json", candle_source=lambda s, i, n: candles)
    tr.step(61 * 3600)
    assert tr.state["positions"]["X-USD"]["stop_id"] is None
    broker.fixed = {"X-USD": Quote("X-USD", 50, 49.99, 50.01)}
    assert tr.check_stops(61 * 3600 + 900) == []
    tr.step(62 * 3600)
    assert "X-USD" in tr.state["positions"]


def test_backtest_contribution_cap():
    res = backtest.run({"X-USD": flat(24 * 30)}, lambda: MeanReversion(), start_equity=0,
                       daily_contribution=25, contribution_cap=100)
    assert res.metrics()["contributed"] == 100
