from __future__ import annotations

from datetime import timedelta
from typing import Optional

from bot.models import Bar
from bot.strategies.base import Strategy


class OpeningRangeBreakout(Strategy):
    """Opening Range Breakout.

    Builds the high/low of the first `range_minutes` after 09:15, then goes long
    on a close above range-high (+buffer) or short on a close below range-low.
    At most one breakout per direction per day.
    """

    name = "orb"
    defaults = {"range_minutes": 15, "buffer_pct": 0.05}

    def reset_day(self) -> None:
        self.fired: set[int] = set()

    def __init__(self, params=None):
        super().__init__(params)
        self.reset_day()

    def on_bar(self, bars: list[Bar], side: int) -> Optional[int]:
        if not bars:
            return None
        day_open = bars[0].start.replace(hour=9, minute=15, second=0, microsecond=0)
        range_end = day_open + timedelta(minutes=int(self.p["range_minutes"]))
        rng = [b for b in bars if day_open <= b.start < range_end]
        last = bars[-1]
        if not rng or last.start < range_end:
            return None
        hi, lo = max(b.high for b in rng), min(b.low for b in rng)
        buf = float(self.p["buffer_pct"]) / 100
        if last.close > hi * (1 + buf) and 1 not in self.fired:
            self.fired.add(1)
            return 1
        if last.close < lo * (1 - buf) and -1 not in self.fired:
            self.fired.add(-1)
            return -1
        return None

    def state(self, bars: list[Bar], ltp: float | None = None) -> dict:
        """The opening range and the closes that would break it."""
        out = super().state(bars, ltp)
        if not bars:
            return out
        day_open = bars[0].start.replace(hour=9, minute=15, second=0, microsecond=0)
        range_end = day_open + timedelta(minutes=int(self.p["range_minutes"]))
        rng = [b for b in bars if day_open <= b.start < range_end]
        out["range_done"] = bool(rng) and bars[-1].start >= range_end
        if rng:
            hi, lo = max(b.high for b in rng), min(b.low for b in rng)
            buf = float(self.p["buffer_pct"]) / 100
            out.update(range_high=round(hi, 2), range_low=round(lo, 2), buy_above=round(hi * (1 + buf), 2),
                       sell_below=round(lo * (1 - buf), 2), fired=sorted(self.fired))
        return out
