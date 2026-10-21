"""Historical candles: fetch from SmartAPI in chunks, cache as CSV, resample."""
from __future__ import annotations

import csv
import time
from datetime import datetime, timedelta
from pathlib import Path

from bot.config import DATA_DIR
from bot.models import Bar, Instrument
from bot.timeutil import IST, floor_to

# SmartAPI max days per request for each interval
MAX_DAYS = {"ONE_MINUTE": 30, "THREE_MINUTE": 60, "FIVE_MINUTE": 100, "TEN_MINUTE": 100,
            "FIFTEEN_MINUTE": 200, "THIRTY_MINUTE": 200, "ONE_HOUR": 400, "ONE_DAY": 2000}


def csv_path(symbol: str, interval: str = "ONE_MINUTE") -> Path:
    return DATA_DIR / "candles" / f"{symbol}_{interval}.csv"


def fetch(broker, inst: Instrument, start: datetime, end: datetime, interval: str = "ONE_MINUTE") -> list[list]:
    """Fetch candles in API-sized chunks. Returns [[iso_ts, o, h, l, c, v], ...]."""
    out, step = [], timedelta(days=MAX_DAYS.get(interval, 30))
    cur = start
    while cur < end:
        chunk_end = min(cur + step, end)
        rows = broker.candles(inst, interval, cur, chunk_end)
        out.extend(rows)
        cur = chunk_end + timedelta(minutes=1)
        time.sleep(0.4)  # stay well under historical-API rate limits
    seen, dedup = set(), []
    for r in out:
        if r[0] not in seen:
            seen.add(r[0]); dedup.append(r)
    dedup.sort(key=lambda r: r[0])
    return dedup


def save_csv(rows: list[list], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {r[0]: r for r in load_csv(path)} if path.exists() else {}
    for r in rows:
        existing[r[0]] = r
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for k in sorted(existing):
            w.writerow(existing[k])


def load_csv(path: Path) -> list[list]:
    with open(path) as f:
        r = csv.reader(f)
        next(r, None)
        return [[row[0], *map(float, row[1:6])] for row in r if row]


def to_bars(rows: list[list]) -> list[Bar]:
    bars = []
    for r in rows:
        ts = datetime.fromisoformat(r[0])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=IST)
        bars.append(Bar(ts.astimezone(IST), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    return bars


def resample(bars: list[Bar], minutes: int) -> list[Bar]:
    if minutes <= 1:
        return bars
    out: list[Bar] = []
    for b in bars:
        start = floor_to(b.start, minutes)
        if out and out[-1].start == start:
            o = out[-1]
            o.high, o.low, o.close, o.volume = max(o.high, b.high), min(o.low, b.low), b.close, o.volume + b.volume
        else:
            out.append(Bar(start, b.open, b.high, b.low, b.close, b.volume))
    return out


def split_days(bars: list[Bar]) -> dict[str, list[Bar]]:
    days: dict[str, list[Bar]] = {}
    for b in bars:
        days.setdefault(b.start.strftime("%Y-%m-%d"), []).append(b)
    return days
