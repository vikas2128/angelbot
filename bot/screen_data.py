"""Collects the screener's raw inputs from the broker: completed daily candles and one batched quote call.

Read-only: market data only, no orders. Needs a logged-in LiveBroker (see `python -m bot.cli screen`).
Anything missing is reported in `errors` and left out, never filled in with a guess.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from bot.models import Instrument

log = logging.getLogger("angelbot.screen_data")


def collect(broker, instruments: list[Instrument], now: datetime, lookback_days: int = 20,
            pause: float = 0.5) -> tuple[list[dict], list[str]]:
    """Return (raw rows for bot.screener.rank, error messages)."""
    errors: list[str] = []
    by_exchange: dict[str, list[str]] = {}
    for i in instruments:
        by_exchange.setdefault(i.exchange, []).append(i.token)
    try:
        quotes = broker.quotes(by_exchange)
    except Exception as e:
        quotes = {}
        errors.append(f"quotes failed, so no gap, volume or spread today: {e}")
    today = now.strftime("%Y-%m-%d")
    start = now - timedelta(days=int(lookback_days * 1.6) + 5)          # calendar days that cover the trading days
    raws = []
    for inst in instruments:
        try:
            rows = broker.candles(inst, "ONE_DAY", start, now)
        except Exception as e:
            errors.append(f"{inst.symbol}: no daily candles ({e})")
            continue
        finally:
            time.sleep(pause)                                         # the history API allows only a few calls a second
        daily = [{"d": str(r[0])[:10], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                 for r in rows if str(r[0])[:10] < today]             # today's candle is still forming
        q = quotes.get(str(inst.token)) or {}
        if quotes and not q:
            errors.append(f"{inst.symbol}: no quote returned")
        depth = q.get("depth") or {}
        first = lambda side: ((depth.get(side) or [{}])[0] or {}).get("price")
        raws.append({"symbol": inst.symbol, "token": inst.token, "ltp": q.get("ltp"), "open": q.get("open"),
                     "prev_close": q.get("close"), "volume": q.get("tradeVolume", q.get("volume")),
                     "bid": first("buy"), "ask": first("sell"), "daily": daily})
    return raws, errors
