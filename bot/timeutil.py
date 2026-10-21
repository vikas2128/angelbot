from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def at(day: datetime, hhmm: str) -> datetime:
    t = parse_hhmm(hhmm)
    return day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)


def is_trading_day(d: datetime, holidays: list[str]) -> bool:
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in set(holidays or [])


def floor_to(ts: datetime, minutes: int) -> datetime:
    """Floor a timestamp to the bar boundary, anchored at 09:15 IST (NSE open)."""
    anchor = ts.replace(hour=9, minute=15, second=0, microsecond=0)
    if ts < anchor:
        anchor -= timedelta(days=1)
    n = int((ts - anchor).total_seconds() // (minutes * 60))
    return anchor + timedelta(minutes=n * minutes)


def from_epoch_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, IST)
