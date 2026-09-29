"""The bot's allowance. It gets `daily` dollars per calendar day since
`start_day` (day one included), up to `cap` in total, plus whatever it has
made or lost. It never spends account cash beyond that, so money you keep in
the account for other things is off limits, and a bad week costs at most
what the bot was given.

    bot cash   = contributed + realized P&L - cost of open bot positions
    bot equity = bot cash + market value of open bot positions

Profits stay in the pot and are re-risked (compounding); position sizes are a
fraction of bot equity, so they grow as the pot grows.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class Budget:
    daily: float = 25.0
    start_day: str = ""  # ISO date, set on first run
    cap: float = 0.0     # total contributions stop growing here; 0 = no cap

    def contributed(self, now: float) -> float:
        today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
        start = dt.date.fromisoformat(self.start_day) if self.start_day else today
        total = self.daily * max(0, (today - start).days + 1)
        return min(total, self.cap) if self.cap else total
