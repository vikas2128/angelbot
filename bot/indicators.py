from __future__ import annotations

from typing import Sequence


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average (seeded with SMA of the first `period` values)."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def vwap(bars) -> float | None:
    """Session VWAP from bars (uses typical price; falls back to equal weights if no volume)."""
    pv = vol = 0.0
    for b in bars:
        tp = (b.high + b.low + b.close) / 3
        w = b.volume if b.volume > 0 else 1.0
        pv += tp * w
        vol += w
    return pv / vol if vol else None


def atr(bars, period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / period
