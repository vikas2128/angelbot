"""Nightly per-stock backtests for the screener's backtest check.

After the close, every stock the screener would look at is backtested with today's strategy, exits, risk limits and
order size over the last `days` days of 1-minute candles. The results go to runtime/<area>/stock_backtests.json.
Next morning, with `screener.backtest_gate: true`, a stock whose backtest lost money after fees, or made too few
trades to judge, is left out of the ranking.

Each stock is also backtested with every other registered strategy on the same candles, for the screener page's
strategy comparison. That comparison is information only: it never changes which strategy the bot uses.

Read-only market data; nothing here places orders. A backtest over a few weeks is a coarse filter against stocks that
clearly lose with this strategy, not proof that a stock will make money: the past month need not repeat.
"""
from __future__ import annotations

import copy
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
           "amount": float((cfg.get("sizing") or {}).get("amount") or 0), "qty": _qty(cfg), "risk": cfg.get("risk"), "charges": cfg.get("charges"),
           "session": {k: v for k, v in (cfg.get("session") or {}).items() if k != "holidays"}}
    return hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:12]


def summarise(res: dict) -> dict:
    keep = ("trades", "net_pnl", "charges", "win_rate_pct", "profit_factor", "max_drawdown", "days")
    return {k: res.get(k) for k in keep}


def run_all(cfg: dict, broker, insts: list, now: datetime, days: int = DEFAULT_DAYS, fetch_fn=None,
            progress=None) -> dict:
    """Fetch candles and backtest each instrument with every strategy. One stock failing never stops the rest."""
    from bot import data as D
    from bot.backtest import run_backtest
    from bot.strategies import STRATEGIES
    fetch_fn = fetch_fn or D.fetch
    current = cfg["strategy"]["name"]
    all_params = cfg["strategy"].get("params") or {}
    end = now.replace(second=0, microsecond=0)
    start = (end - timedelta(days=days)).replace(hour=9, minute=15)
    results, compare, errors = {}, {}, []
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
            per = {}
            for name in STRATEGIES:                                  # same candles, size and exits; only the strategy differs
                per[name] = summarise(run_backtest(cfg, bars, qty=_qty(cfg), strategy=name, params=all_params.get(name, {})))
            compare[inst.symbol] = per
            results[inst.symbol] = per[current]
        except Exception as e:                                       # network, rate limit, bad data
            errors.append(f"{inst.symbol}: {e}")
    st = cfg["strategy"]
    return {"run_at": now.isoformat(timespec="seconds"), "days": days, "strategy": st["name"],
            "amount": float((cfg.get("sizing") or {}).get("amount") or 0), "qty": _qty(cfg),
            "fingerprint": fingerprint(cfg), "results": results, "compare": compare, "errors": errors}


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


def totals(compare: dict) -> list[dict]:
    """Per strategy, over all stocks: trades, net after fees, fees, win rate, and how many stocks it made money on."""
    names: list[str] = []
    for per in compare.values():
        names += [n for n in per if n not in names]
    out = []
    for name in names:
        rows = [per[name] for per in compare.values() if name in per]
        trades = sum(int(r.get("trades") or 0) for r in rows)
        wins = sum(round(float(r.get("win_rate_pct") or 0) * int(r.get("trades") or 0) / 100) for r in rows)
        out.append({"strategy": name, "stocks": len(rows), "trades": trades,
                    "net_pnl": round(sum(float(r.get("net_pnl") or 0) for r in rows), 2),
                    "charges": round(sum(float(r.get("charges") or 0) for r in rows), 2),
                    "win_rate_pct": round(100 * wins / trades, 1) if trades else 0.0,
                    "stocks_up": sum(1 for r in rows if float(r.get("net_pnl") or 0) > 0)})
    return sorted(out, key=lambda r: -r["net_pnl"])


def comparison(cfg: dict, now: datetime) -> dict | None:
    """The latest strategy comparison for display, with a note when it is old or was made with other settings."""
    snap = control.read_json(path())
    compare = snap.get("compare") or {}
    if not compare or not snap.get("run_at"):
        return None
    notes = []
    try:
        age = (now - datetime.fromisoformat(snap["run_at"])).total_seconds() / 86400
        if age > MAX_AGE_DAYS:
            notes.append(f"{age:.0f} days old")
    except (TypeError, ValueError):
        return None
    same = copy.deepcopy(cfg)                                         # every strategy was run, so a strategy switch is fine
    same["strategy"]["name"] = snap.get("strategy") or cfg["strategy"]["name"]
    if snap.get("fingerprint") != fingerprint(same):
        notes.append("made before the exits, risk limits, order size or strategy parameters last changed")
    return {"run_at": snap["run_at"], "days": snap.get("days"), "current": cfg["strategy"]["name"],
            "totals": totals(compare), "by_stock": compare, "note": "; ".join(notes)}
