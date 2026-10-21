"""Risk manager: every entry must pass here. Exits are never blocked."""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta

from bot.timeutil import at


class OrderThrottle:
    """Sliding-window limiter on order actions (place/modify/cancel)."""

    def __init__(self, max_per_sec: float):
        self.max = max(1, int(max_per_sec))
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.events and now - self.events[0] >= 1.0:
                    self.events.popleft()
                if len(self.events) < self.max:
                    self.events.append(now)
                    return
                wait = 1.0 - (now - self.events[0])
            time.sleep(max(wait, 0.01))


class RiskManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.r = cfg["risk"]
        self.s = cfg["session"]
        self.trades_today = 0
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.halted_reason: str | None = None

    # ---- state updates ----
    def on_trade_closed(self, pnl: float, now: datetime) -> None:
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= int(self.r["max_consecutive_losses"]):
                self.cooldown_until = now + timedelta(minutes=int(self.r["cooldown_minutes"]))
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

    def check_pnl_limits(self, day_pnl: float) -> str | None:
        """Return a halt reason if a daily P&L limit is breached."""
        if self.halted_reason:
            return self.halted_reason
        if day_pnl <= -abs(float(self.r["max_daily_loss"])):
            self.halted_reason = f"max_daily_loss hit (₹{day_pnl:.2f})"
        elif float(self.r.get("max_daily_profit") or 0) > 0 and day_pnl >= float(self.r["max_daily_profit"]):
            self.halted_reason = f"max_daily_profit reached (₹{day_pnl:.2f}) — locking in"
        return self.halted_reason

    # ---- gates ----
    def can_enter(self, now: datetime, open_positions: int, order_value: float) -> tuple[bool, str]:
        if self.halted_reason:
            return False, f"halted: {self.halted_reason}"
        if now < at(now, self.s["entry_start"]):
            return False, "before entry_start"
        if now >= at(now, self.s["entry_cutoff"]):
            return False, "after entry_cutoff"
        if self.trades_today >= int(self.r["max_trades_per_day"]):
            return False, "max_trades_per_day reached"
        if open_positions >= int(self.r["max_open_positions"]):
            return False, "max_open_positions reached"
        if order_value > float(self.r["max_order_value"]):
            return False, f"order value ₹{order_value:.0f} > max_order_value"
        if self.cooldown_until and now < self.cooldown_until:
            return False, f"cooldown until {self.cooldown_until:%H:%M}"
        return True, "ok"

    def must_squareoff(self, now: datetime) -> bool:
        return now >= at(now, self.s["squareoff"])

    def should_shutdown(self, now: datetime) -> bool:
        return now >= at(now, self.s["shutdown"])
