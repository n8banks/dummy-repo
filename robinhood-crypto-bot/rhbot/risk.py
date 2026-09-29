"""Risk management. Every entry, in backtest and live, goes through here.

Loss cap: every position's stop is at most `max_stop_pct` (5%) below the
price paid, so no single trade is allowed to lose more than that. The one
exception is a price gap straight through the stop (a crash between checks),
where the sell fills at the first available price.

Sizing rule (fixed-fractional risk): pick a stop distance from volatility
(k * ATR, capped at 5%), then size the position so that hitting the stop loses at most
`risk_per_trade` of equity, including the spread paid. Then clamp by the
per-position cap, total exposure cap and available cash.

Circuit breakers: stop opening new positions after the day's loss exceeds
`max_daily_loss`, and stop everything after equity falls `max_drawdown`
below its peak. Both require a human to restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.005   # lose at most 0.5% of equity per stopped-out trade
    stop_atr_mult: float = 2.5      # stop distance = 2.5 * ATR ...
    max_stop_pct: float = 0.05      # ... but never more than 5% below the entry price
    take_profit_r: float = 0.0      # 0 = no fixed target; exits come from trailing stop / signal
    trail_atr_mult: float = 3.0     # trailing stop distance once in profit
    max_position_pct: float = 0.10  # no single coin above 10% of equity: many small bets
    max_exposure_pct: float = 0.70  # at most 70% of equity in crypto at once
    max_positions: int = 8
    max_daily_loss: float = 0.03    # pause new entries for the day after -3%
    max_drawdown: float = 0.15      # hard stop at -15% from peak equity
    min_order_usd: float = 1.0      # Robinhood has no per-order fee, so small orders are fine
    max_cost_to_stop: float = 0.35  # skip if round-trip spread eats >35% of the stop distance


@dataclass
class PositionSize:
    quantity: float
    notional: float
    stop_price: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.quantity > 0


def size_position(cfg: RiskConfig, equity: float, cash: float, exposure: float,
                  open_positions: int, entry_price: float, atr_value: float,
                  spread_pct: float) -> PositionSize:
    """How much to buy. `spread_pct` is the round-trip cost fraction
    (ask - bid) / mid; `exposure` is the current notional held."""
    none = lambda why: PositionSize(0.0, 0.0, 0.0, why)  # noqa: E731
    if open_positions >= cfg.max_positions:
        return none("max positions reached")
    if atr_value <= 0 or entry_price <= 0:
        return none("no volatility estimate")

    stop_dist = min(cfg.stop_atr_mult * atr_value, cfg.max_stop_pct * entry_price)
    stop_price = entry_price - stop_dist
    if stop_price <= 0:
        return none("stop below zero")
    stop_frac = stop_dist / entry_price
    # If the spread is a big chunk of the stop distance the trade can't pay for itself.
    if spread_pct > cfg.max_cost_to_stop * stop_frac:
        return none(f"spread {spread_pct:.2%} too wide vs stop {stop_frac:.2%}")

    loss_per_unit = stop_dist + spread_pct * entry_price
    qty = cfg.risk_per_trade * equity / loss_per_unit

    caps = [
        cfg.max_position_pct * equity,
        cfg.max_exposure_pct * equity - exposure,
        cash * 0.995,
    ]
    notional = min(qty * entry_price, *caps)
    if notional < cfg.min_order_usd:
        return none("below minimum order / no room under caps")
    return PositionSize(notional / entry_price, notional, stop_price, "ok")


@dataclass
class CircuitBreaker:
    cfg: RiskConfig
    peak_equity: float = 0.0
    day: int = -1
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    events: list[str] = field(default_factory=list)

    def update(self, ts: int, equity: float) -> None:
        d = ts // 86400
        if d != self.day:
            self.day, self.day_start_equity = d, equity
        self.peak_equity = max(self.peak_equity, equity)
        if not self.halted and self.peak_equity and equity < self.peak_equity * (1 - self.cfg.max_drawdown):
            self.halted = True
            self.halt_reason = f"drawdown {1 - equity / self.peak_equity:.1%} exceeded limit"
            self.events.append(f"{ts}: HALT {self.halt_reason}")

    def can_enter(self, equity: float) -> bool:
        if self.halted:
            return False
        if self.day_start_equity and equity < self.day_start_equity * (1 - self.cfg.max_daily_loss):
            return False
        return True
