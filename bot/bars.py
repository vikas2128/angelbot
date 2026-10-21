"""Builds fixed-interval OHLC bars from ticks, per instrument token."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from bot.models import Bar, Tick
from bot.timeutil import floor_to


class BarBuilder:
    def __init__(self, minutes: int, on_bar: Callable[[str, Bar], None]):
        self.minutes = minutes
        self.on_bar = on_bar
        self.current: dict[str, Bar] = {}
        self.last_cum_volume: dict[str, float] = {}

    def on_tick(self, tick: Tick, cum_volume: float | None = None) -> None:
        start = floor_to(tick.ts, self.minutes)
        bar = self.current.get(tick.token)
        if bar is not None and start > bar.start:
            self._close(tick.token)
            bar = None
        if bar is not None and start < bar.start:
            return  # late/out-of-order tick for an already-closed bar; ignore
        vol = 0.0
        if cum_volume is not None:
            prev = self.last_cum_volume.get(tick.token)
            vol = max(cum_volume - prev, 0.0) if prev is not None else 0.0
            self.last_cum_volume[tick.token] = cum_volume
        if bar is None:
            self.current[tick.token] = Bar(start, tick.ltp, tick.ltp, tick.ltp, tick.ltp, vol, 1)
        else:
            bar.high = max(bar.high, tick.ltp)
            bar.low = min(bar.low, tick.ltp)
            bar.close = tick.ltp
            bar.volume += vol
            bar.ticks += 1

    def flush(self, now: datetime, grace_sec: float = 2.0) -> None:
        """Close bars whose interval ended (call periodically; handles illiquid symbols)."""
        for token, bar in list(self.current.items()):
            if now >= bar.start + timedelta(minutes=self.minutes, seconds=grace_sec):
                self._close(token)

    def _close(self, token: str) -> None:
        bar = self.current.pop(token, None)
        if bar is not None:
            self.on_bar(token, bar)
