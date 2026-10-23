"""Nightly per-stock backtests for the screener's backtest check.

After the close, every stock the screener would look at is backtested with today's strategy, exits, risk limits and
order size over the last `days` days of 1-minute candles. The results go to runtime/<area>/stock_backtests.json.
Next morning, with `screener.backtest_gate: true`, a stock whose backtest lost money after fees, or made too few
trades to judge, is left out of the ranking.

Read-only market data; nothing here places orders. A backtest over a few weeks is a coarse filter against stocks that
clearly lose with this strategy, not proof that a stock will make money: the past month need not repeat.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from bot import config as C
from bot import control

DEFAULT_DAYS = 30
MAX_AGE_DAYS = 4             # Friday evening's results still count on Monday (and after a one-day holiday)


def path() -> Path:
    return C.RUNTIME_DIR / "stock_backtests.json"


def _qty(cfg: dict) -> int:
    sel = cfg.get("selection") or {}
    if sel.get("mode", "off") != "off":
        return int(sel.get("qty") or 1)
    insts = cfg.get("instruments") or []
    return int(insts[0].get("qty", 1)) if insts else 1


def fingerprint(cfg: dict) -> str:
    """What the results depend on. A change to any of these makes old results stale."""
    st = cfg["strategy"]
    key = {"strategy": st["name"], "params": (st.get("params") or {}).get(st["name"], {}),
           "timeframe": st.get("timeframe_min", 1), "short": bool(st.get("allow_short")), "exits": cfg.get("exits"),
           "amount": float((cfg.get("sizing") or {}).get("amount") or 0), "qty": _qty(cfg), "risk": cfg.get("risk"),
           "session": {k: v for k, v in (cfg.get("session") or {}).items() if k != "holidays"}}
    return hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:12]


def summarise(res: dict) -> dict:
    keep = ("trades", "net_pnl", "charges", "win_rate_pct", "profit_factor", "max_drawdown", "days")
    return {k: res.get(k) for k in keep}


def run_all(cfg: dict, broker, insts: list, now: datetime, days: int = DEFAULT_DAYS, fetch_fn=None,
            progress=None) -> dict:
    """Fetch candles and backtest each instrument. One stock failing never stops the rest."""
    from bot import data as D
    from bot.backtest import run_backtest
    fetch_fn = fetch_fn or D.fetch
    end = now.replace(second=0, microsecond=0)
    start = (end - timedelta(days=days)).replace(hour=9, minute=15)
    results, errors = {}, []
    for n, inst in enumerate(insts, 1):
        if progress:
            progress(n, len(insts), inst.symbol)
        try:
            rows = fetch_fn(broker, inst, start, end)
            if rows:
                D.save_csv(rows, D.csv_path(inst.symbol))           # also a cache for hand-run backtests
            bars = D.to_bars(rows)
            if not bars:
                errors.append(f"{inst.symbol}: no candles")
                continue
            results[inst.symbol] = summarise(run_backtest(cfg, bars, qty=_qty(cfg)))
        except Exception as e:                                       # network, rate limit, bad data
            errors.append(f"{inst.symbol}: {e}")
    st = cfg["strategy"]
    return {"run_at": now.isoformat(timespec="seconds"), "days": days, "strategy": st["name"],
            "amount": float((cfg.get("sizing") or {}).get("amount") or 0), "qty": _qty(cfg),
            "fingerprint": fingerprint(cfg), "results": results, "errors": errors}


def save(snapshot: dict) -> None:
    control.write_json(path(), snapshot)


def load_fresh(cfg: dict, now: datetime) -> tuple[dict | None, str]:
    """(results by symbol, "") if there are usable results for today's settings, else (None, why not)."""
    snap = control.read_json(path())
    if not snap.get("run_at"):
        return None, "no stock backtests yet"
    try:
        age = (now - datetime.fromisoformat(snap["run_at"])).total_seconds() / 86400
    except (TypeError, ValueError):
        return None, "the stock backtests file is damaged"
    if age > MAX_AGE_DAYS:
        return None, f"the stock backtests are {age:.0f} days old"
    if snap.get("fingerprint") != fingerprint(cfg):
        return None, "the strategy, exits or order size changed since the stock backtests were run"
    return snap.get("results") or {}, ""
