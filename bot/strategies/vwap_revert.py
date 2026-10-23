from __future__ import annotations

from typing import Optional

from bot.indicators import vwap
from bot.models import Bar
from bot.strategies.base import Strategy


class VwapReversion(Strategy):
    """Mean reversion to session VWAP.

    Long when close is `band_pct`% below VWAP, short when above; optionally
    exit when price crosses back through VWAP.
    """

    name = "vwap_revert"
    defaults = {"band_pct": 0.4, "exit_at_vwap": True}
    warmup_bars = 5

    def on_bar(self, bars: list[Bar], side: int) -> Optional[int]:
        if len(bars) < self.warmup_bars:
            return None
        v = vwap(bars)
        if not v:
            return None
        px = bars[-1].close
        dev = (px - v) / v * 100
        band = float(self.p["band_pct"])
        if side == 0:
            if dev <= -band:
                return 1
            if dev >= band:
                return -1
            return None
        if self.p.get("exit_at_vwap"):
            if side > 0 and px >= v:
                return 0
            if side < 0 and px <= v:
                return 0
        return None

    def state(self, bars: list[Bar], ltp: float | None = None) -> dict:
        """Today's VWAP, how far the price is from it, and the buy/sell levels."""
        out = super().state(bars, ltp)
        v = vwap(bars) if bars else None
        if not v:
            return out
        band = float(self.p["band_pct"]) / 100
        ref = ltp or bars[-1].close
        out.update(vwap=round(v, 2), dev_pct=round((ref - v) / v * 100, 3),
                   buy_below=round(v * (1 - band), 2), sell_above=round(v * (1 + band), 2))
        return out
