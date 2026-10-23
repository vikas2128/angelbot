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
            pause: float = 1.0, progress=None) -> tuple[list[dict], list[str]]:
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
    raws = []
    for n, inst in enumerate(instruments, 1):
        if progress:
            progress(n, len(instruments), inst.symbol)
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
    raws, errs = collect(broker, insts, now, int(p["lookback_days"]), pause=pause, progress=progress)
    if p.get("backtest_gate"):                                     # attach last night's per-stock backtests
        from bot import stock_backtest as SB
        results, why = SB.load_fresh(cfg, now)
        if results is None:
            p["bt_unavailable"] = f"{why}; run  ./deploy/ab <area> backtest-stocks  after the close"
            errs = errs + [f"Backtest check: {p['bt_unavailable']}. Every stock is left out until it runs."]
        for raw in raws:
            raw["bt"] = (results or {}).get(raw["symbol"])
    snap = screener.rank(raws, p, cfg.get("charges", {}), now, errors + errs)
    snap["lists"] = lists                                          # which index lists were screened, and how old they are
    for r in snap["rows"]:
        r["indices"] = member.get(r["symbol"], [])                 # e.g. ["N50", "BANK"]; empty for a hand-picked symbol
    return snap
