"""Collects the screener's raw inputs: completed daily candles and one batched quote call.

Daily candles come from the 1-minute candles the nightly per-stock job saves in data/candles (no API call) when they
reach the last completed trading day; otherwise from the broker's history API, one stock a second. So at 09:16 the
screen normally needs only the quote call, which SmartAPI does not throttle the way it throttles history at the open.

Read-only: market data only, no orders. Needs a logged-in LiveBroker (see `python -m bot.cli screen`).
Anything missing is reported in `errors` and left out, never filled in with a guess.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from bot.models import Instrument

log = logging.getLogger("angelbot.screen_data")


LAST_BAR_FROM = "15:00"       # a cached day counts as complete only if its candles reach the last half hour


def last_trading_day(now: datetime, holidays: list[str] | None = None) -> str:
    """The most recent trading day before `now`'s date (YYYY-MM-DD)."""
    from bot.timeutil import is_trading_day
    d = now - timedelta(days=1)
    while not is_trading_day(d, holidays or []):
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def daily_from_cache(symbol: str, today: str, need_last: str) -> list[dict] | None:
    """Completed daily candles built from the saved 1-minute candles, or None when the file is missing, unreadable or
    does not reach `need_last` (then the caller asks the broker). A day whose candles stop before 15:00 (a fetch made
    during the session) is left out. The day's close is its last minute's close, close to the exchange's official
    close; the screen takes yesterday's official close for the gap from the live quote, not from here."""
    from bot import data as D
    try:
        rows = D.load_csv(D.csv_path(symbol))
    except (OSError, ValueError, IndexError):
        return None
    days: dict[str, dict] = {}
    for ts, o, h, l, c, v in rows:
        d, hm = str(ts)[:10], str(ts)[11:16]
        if d >= today:
            continue
        x = days.get(d)
        if x is None:
            days[d] = {"d": d, "o": o, "h": h, "l": l, "c": c, "v": v, "last": hm}
        else:
            x["h"], x["l"], x["v"] = max(x["h"], h), min(x["l"], l), x["v"] + v
            if hm >= x["last"]:
                x["c"], x["last"] = c, hm
    out = [dict((k, v) for k, v in x.items() if k != "last") for x in sorted(days.values(), key=lambda x: x["d"])
           if x["last"] >= LAST_BAR_FROM]
    if not out or out[-1]["d"] < need_last:
        return None
    return out


def collect(broker, instruments: list[Instrument], now: datetime, lookback_days: int = 20,
            pause: float = 1.0, progress=None, holidays: list[str] | None = None,
            use_cache: bool = True) -> tuple[list[dict], list[str]]:
    """Return (raw rows for bot.screener.rank, error messages).

    Calls are paced at one a second (as bot.data.fetch does): SmartAPI rate-limits the history and quote endpoints
    per account, and the running bot shares that allowance. `progress(i, n, symbol)` is called before each stock.
    """
    errors: list[str] = []
    by_exchange: dict[str, list[str]] = {}
    for i in instruments:
        by_exchange.setdefault(i.exchange, []).append(i.token)
    time.sleep(pause)                                                 # the login call just happened: give the limiter room
    try:
        quotes = broker.quotes(by_exchange)
    except Exception as e:
        quotes = {}
        errors.append(f"quotes failed, so no gap, volume or spread today: {e}")
    time.sleep(pause)
    today = now.strftime("%Y-%m-%d")
    start = now - timedelta(days=int(lookback_days * 1.6) + 5)          # calendar days that cover the trading days
    need_last = last_trading_day(now, holidays)
    raws, cached = [], 0
    for n, inst in enumerate(instruments, 1):
        if progress:
            progress(n, len(instruments), inst.symbol)
        daily = daily_from_cache(inst.symbol, today, need_last) if use_cache else None
        if daily is not None:
            cached += 1
        else:
            try:
                rows = broker.candles(inst, "ONE_DAY", start, now)
            except Exception as e:
                errors.append(f"{inst.symbol}: no daily candles ({e})")
                continue
            finally:
                time.sleep(pause)                                     # the history API allows only a few calls a second
            daily = [{"d": str(r[0])[:10], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
                     for r in rows if str(r[0])[:10] < today]         # today's candle is still forming
        q = quotes.get(str(inst.token)) or {}
        if quotes and not q:
            errors.append(f"{inst.symbol}: no quote returned")
        depth = q.get("depth") or {}
        first = lambda side: ((depth.get(side) or [{}])[0] or {}).get("price")
        raws.append({"symbol": inst.symbol, "token": inst.token, "ltp": q.get("ltp"), "open": q.get("open"),
                     "prev_close": q.get("close"), "volume": q.get("tradeVolume", q.get("volume")),
                     "bid": first("buy"), "ask": first("sell"), "daily": daily})
    log.info("screen data: %d of %d stocks' daily history from saved candles, %d from the broker",
             cached, len(instruments), len(instruments) - cached)
    return raws, errors


def run_screen(cfg: dict, broker, master: list[dict], now: datetime, universe: list[str] | None = None,
               top_n: int | None = None, progress=None, pause: float = 1.0) -> dict:
    """Resolve the universe, collect market data, and return the ranked snapshot (read-only)."""
    from bot import screener
    from bot.instruments import resolve

    p = screener.params_from(cfg)
    if top_n:
        p["top_n"] = top_n
    from bot import universe as U

    # presets (nifty50, banknifty) become their stocks; plain symbols pass through; a stale or missing list is noted, not fatal
    entries = list(universe or (cfg.get("screener") or {}).get("universe") or screener.DEFAULT_UNIVERSE)
    symbols, errors = U.expand(entries)
    lists, member = U.membership(entries)
    insts = []
    for sym in symbols:
        try:
            insts.append(resolve([{"symbol": sym, "exchange": "NSE", "qty": 1}], master)[0])
        except ValueError:
            errors.append(f"{sym}: not found in the instrument master")
    if not insts:
        raise ValueError("no stocks to screen: " + "; ".join(errors))
    raws, errs = collect(broker, insts, now, int(p["lookback_days"]), pause=pause, progress=progress,
                         holidays=(cfg.get("session") or {}).get("holidays"))
    if p.get("strategy_per_stock"):                                # attach last night's per-stock strategy choices
        from bot import stock_backtest as SB
        choices, why = SB.load_choices(cfg, now)
        if choices is None:
            p["bt_unavailable"] = f"{why}; run  ./deploy/ab <area> backtest-stocks  after the close"
            errs = errs + [f"Strategy check: {p['bt_unavailable']}. Every stock is left out until it runs."]
        for raw in raws:
            raw["choice"] = (choices or {}).get(raw["symbol"])
    elif p.get("backtest_gate"):                                   # attach last night's per-stock backtests
        from bot import stock_backtest as SB
        results, why = SB.load_fresh(cfg, now)
        if results is None:
            p["bt_unavailable"] = f"{why}; run  ./deploy/ab <area> backtest-stocks  after the close"
            errs = errs + [f"Backtest check: {p['bt_unavailable']}. Every stock is left out until it runs."]
        for raw in raws:
            raw["bt"] = (results or {}).get(raw["symbol"])
    snap = screener.rank(raws, p, cfg.get("charges", {}), now, errors + errs)
    from bot import stock_backtest as SB
    snap["strategy_compare"] = SB.comparison(cfg, now)              # information only: never changes the strategy
    snap["lists"] = lists                                          # which index lists were screened, and how old they are
    for r in snap["rows"]:
        r["indices"] = member.get(r["symbol"], [])                 # e.g. ["N50", "BANK"]; empty for a hand-picked symbol
    return snap
