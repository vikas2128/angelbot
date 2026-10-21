from __future__ import annotations

from typing import Optional

from bot.indicators import ema
from bot.models import Bar
from bot.strategies.base import Strategy


class EmaCross(Strategy):
    """Go long when fast EMA crosses above slow EMA; short/flat on cross below.

    Signals fire only on the crossover bar, so after a stop-out the bot
    waits for the next cross instead of immediately re-entering.
    """

    name = "ema_cross"
    defaults = {"fast": 9, "slow": 21}

    @property
    def warmup_bars(self) -> int:  # type: ignore[override]
        return int(self.p["slow"]) + 1

    def on_bar(self, bars: list[Bar], side: int) -> Optional[int]:
        fast, slow = int(self.p["fast"]), int(self.p["slow"])
        if fast >= slow:
            raise ValueError("ema_cross: fast must be < slow")
        closes = [b.close for b in bars]
        if len(closes) < slow + 1:
            return None
        f, s = ema(closes, fast), ema(closes, slow)
        # align tails
        f_now, f_prev = f[-1], f[-2]
        s_now, s_prev = s[-1], s[-2]
        if f_prev <= s_prev and f_now > s_now:
            return 1
        if f_prev >= s_prev and f_now < s_now:
            return -1
        return None
