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

    def state(self, bars: list[Bar], ltp: float | None = None) -> dict:
        """Both EMAs, which is above, and the next 1-bar close that would make them cross."""
        out = super().state(bars, ltp)
        fast, slow = int(self.p["fast"]), int(self.p["slow"])
        closes = [b.close for b in bars]
        if len(closes) < slow + 1 or fast >= slow:
            return out
        f, s = ema(closes, fast)[-1], ema(closes, slow)[-1]
        kf, ks = 2 / (fast + 1), 2 / (slow + 1)
        # next close x where both EMAs meet: f + kf(x - f) = s + ks(x - s)
        cross = (s * (1 - ks) - f * (1 - kf)) / (kf - ks)
        ref = ltp or closes[-1]
        out.update(fast_ema=round(f, 2), slow_ema=round(s, 2), fast_above=f > s,
                   gap_pct=round((f - s) / s * 100, 3) if s else None,
                   next_signal="sell" if f > s else "buy", cross_price=round(cross, 2),
                   cross_move_pct=round((cross - ref) / ref * 100, 3) if ref else None)
        return out
