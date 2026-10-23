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


BOT_START = "09:05"          # deploy/angelbot@.timer starts the bot at this time on weekdays
MARKET_OPEN, MARKET_CLOSE = "09:15", "15:30"


def market_clock(now: datetime, session: dict) -> dict:
    """For screens: is the market open now, is today's bot session over, and when does the bot next start."""
    holidays = session.get("holidays") or []
    shutdown = session.get("shutdown") or MARKET_CLOSE
    trading = is_trading_day(now, holidays)
    d = now
    for _ in range(15):                                  # the next trading day whose start is still ahead
        if is_trading_day(d, holidays) and at(d, BOT_START) > now:
            break
        d = (d + timedelta(days=1)).replace(hour=0, minute=0)
    return {"trading_day": trading,
            "open": trading and at(now, MARKET_OPEN) <= now < at(now, MARKET_CLOSE),
            "session_over": (not trading) or now >= at(now, shutdown),
            "next_start": at(d, BOT_START).isoformat()}
