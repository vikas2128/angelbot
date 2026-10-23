"""AngelBot MCP server for Claude Code (stdio).

Gives Claude visibility and *bounded* control over the bot:
  read   : status, trades, journal (read-only SQL), logs, config, data, strategies
  analyse: backtests and small parameter sweeps on cached candles
  control: pause / resume / kill-switch (flatten)

It deliberately has NO tool to place, modify or cancel individual orders and NO way
to switch paper→live. Trading decisions stay with the coded strategy + risk rules.
"""
from __future__ import annotations

import copy
import itertools
import json
import subprocess
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Work on one area's files. Without ANGELBOT_RUNTIME: use runtime/<ANGELBOT_PROFILE>, or the paper area once it exists
# (so a server started without .mcp.json, such as over SSH, still reads the paper area), or the old runtime/ folder.
import os  # noqa: E402

if "ANGELBOT_RUNTIME" not in os.environ:
    _area = os.environ.get("ANGELBOT_PROFILE") or ("paper" if (ROOT / "runtime" / "paper").is_dir() else "")
    if _area:
        os.environ["ANGELBOT_PROFILE"] = _area
        os.environ["ANGELBOT_RUNTIME"] = str(ROOT / "runtime" / _area)

from bot import config as C  # noqa: E402
from bot import control  # noqa: E402
from bot import settings as ST  # noqa: E402
from bot.journal import query_readonly  # noqa: E402

try:  # MCP Python SDK v2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # v1.x
    from mcp.server.fastmcp import FastMCP as _Server

C.load_dotenv()
mcp = _Server("angelbot", instructions=(
    "Tools to monitor and analyse the AngelBot intraday trading bot (Angel One SmartAPI). "
    "Use bot_status first. Never claim a strategy is profitable from a single backtest; "
    "report trades, net P&L after charges, drawdown and win rate. kill_switch flattens all "
    "positions — use only when the user explicitly asks."
))


def _safe(fn):
    """Return errors as {"error": ...} so Claude sees the message instead of a generic failure."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
    return wrapper


def _cfg() -> dict:
    return ST.load()


# ───────────────────────────── read ─────────────────────────────
@mcp.tool()
def bot_status() -> dict:
    """Live bot status: alive?, mode (paper/live), day P&L, positions, pending orders,
    halts, feed health, recent signals, the current control flags, and strategy_state: per instrument, what the
    strategy sees now (ema_cross: bars, fast/slow EMA, which is above, cross_price = the next 1-minute close that
    would make them cross, and cross_move_pct from the last price; orb: range and break levels; vwap_revert: VWAP
    and bands). strategy_state is display only."""
    s = control.read_status()
    s["control"] = control.read_control()
    try:
        s["mode_configured"] = C.trading_mode()
    except RuntimeError as e:
        s["mode_configured"] = f"invalid: {e}"
    return s


@mcp.tool()
def service_status() -> dict:
    """systemd state of this area's units (OCI host): the trading bot and its morning timer, the Telegram controller,
    and the nightly per-stock backtest timer. Old pre-area units are listed only if they are still active or failed."""
    area = os.environ.get("ANGELBOT_PROFILE") or "paper"
    units = [f"angelbot@{area}.service", f"angelbot@{area}.timer", f"angelbot-telegram@{area}.service",
             f"angelbot-backtest@{area}.timer", f"angelbot-backtest@{area}.service"]
    out: dict = {"area": area}

    def state(unit: str) -> str:
        try:
            r = _SYSTEMCTL(["systemctl", "--user", "is-active", unit], capture_output=True, text=True, timeout=5)
            return r.stdout.strip() or r.stderr.strip()
        except Exception as e:
            return f"unavailable: {e}"

    for unit in units:
        out[unit] = state(unit)
    legacy = {u: s for u in ("angelbot.service", "angelbot.timer", "angelbot-telegram.service")
              if (s := state(u)) in ("active", "activating", "failed")}
    if legacy:
        out["legacy_units"] = {**legacy, "note": "pre-area units: disable with "
                               "systemctl --user disable --now angelbot.timer angelbot.service angelbot-telegram.service; "
                               "a 'failed' one is only an old record (systemctl --user reset-failed clears it)"}
    try:
        r = _SYSTEMCTL(["systemctl", "--user", "list-timers", f"angelbot@{area}.timer", f"angelbot-backtest@{area}.timer",
                        "--all", "--no-pager"], capture_output=True, text=True, timeout=5)
        out["timers"] = r.stdout.strip()
    except Exception:
        pass
    return out


_SYSTEMCTL = subprocess.run          # replaced in tests


@mcp.tool()
@_safe
def trades(date: str | None = None, limit: int = 50) -> dict:
    """Closed round-trip trades from the journal. date=YYYY-MM-DD (default: all, newest first).
    Returns rows plus totals (gross, charges, net, win rate)."""
    if date and (len(date) != 10 or not date.replace("-", "").isdigit()):
        raise ValueError("date must be YYYY-MM-DD")
    where = f"WHERE substr(exit_ts,1,10)='{date}'" if date else ""
    rows = query_readonly(control.JOURNAL, f"SELECT * FROM trades {where} ORDER BY exit_ts DESC", limit)
    n = len(rows)
    return {
        "count": n,
        "net": round(sum(r["net_pnl"] or 0 for r in rows), 2),
        "gross": round(sum(r["gross_pnl"] or 0 for r in rows), 2),
        "charges": round(sum(r["charges"] or 0 for r in rows), 2),
        "win_rate_pct": round(100 * sum(1 for r in rows if (r["net_pnl"] or 0) > 0) / n, 1) if n else None,
        "rows": rows,
    }


@mcp.tool()
@_safe
def journal_query(sql: str, limit: int = 200) -> dict:
    """Run ONE read-only SELECT against the journal (SQLite).
    Tables: trades(mode,symbol,side,qty,entry_ts,entry_price,exit_ts,exit_price,gross_pnl,charges,net_pnl,exit_reason,strategy),
    orders(order_id,ts,mode,symbol,side,qty,price,purpose,status,filled_qty,avg_fill,chases,reason,updated),
    signals(ts,symbol,desired,side_before,price,acted,reason), events(ts,level,kind,msg,data)."""
    rows = query_readonly(control.JOURNAL, sql, min(limit, 1000))
    return {"count": len(rows), "rows": rows}


@mcp.tool()
def tail_log(lines: int = 100, contains: str | None = None) -> str:
    """Last N lines of runtime/bot.log, optionally filtered by a substring (e.g. 'ERROR', 'FILL')."""
    if not control.LOG.exists():
        return "(no log yet)"
    dq: deque[str] = deque(maxlen=min(lines, 2000))
    with open(control.LOG, errors="replace") as f:
        for line in f:
            if contains is None or contains in line:
                dq.append(line.rstrip())
    return "\n".join(dq)


@mcp.tool()
def get_config() -> str:
    """Current config/config.yaml text (strategy, risk, session, instruments), plus the settings changed from
    Telegram for this area (runtime/<area>/settings.yaml), which override config.yaml."""
    text = C.CONFIG_PATH.read_text()
    lines = ST.summary()
    if lines:
        text += ("\n# ── Overridden from Telegram for this area (runtime settings.yaml; these win over the values above) ──\n"
                 + "".join(f"#   {l}\n" for l in lines))
    return text


@mcp.tool()
def validate_config() -> dict:
    """Validate config/config.yaml after editing it. The bot reads config at start-up, so
    changes apply from the next session (or after `systemctl --user restart angelbot`)."""
    try:
        cfg = ST.load()
        return {"valid": True, "telegram_overrides": ST.summary(), "strategy": cfg["strategy"]["name"],
                "instruments": [i["symbol"] for i in cfg["instruments"]]}
    except Exception as e:
        return {"valid": False, "error": str(e)}


@mcp.tool()
def list_strategies() -> dict:
    """Available strategies with default params and description."""
    from bot.strategies import STRATEGIES
    return {n: {"defaults": c.defaults, "doc": (c.__doc__ or "").strip()} for n, c in STRATEGIES.items()}


@mcp.tool()
def list_data() -> list[dict]:
    """Cached candle CSVs in data/candles with row counts and date ranges."""
    out = []
    for p in sorted((C.DATA_DIR / "candles").glob("*.csv")):
        with open(p) as f:
            lines = f.read().splitlines()[1:]
        out.append({"file": p.name, "rows": len(lines),
                    "from": lines[0][:10] if lines else None, "to": lines[-1][:10] if lines else None})
    return out


# ───────────────────────────── analyse ─────────────────────────────
def _bars(symbol: str, date_from: str | None, date_to: str | None, csv: str | None):
    from bot.data import csv_path, load_csv, to_bars
    path = Path(csv) if csv else csv_path(symbol)
    if not path.is_absolute():
        path = C.DATA_DIR / "candles" / path if not (ROOT / path).exists() else ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"no candles at {path}; call fetch_candles('{symbol}') first")
    bars = to_bars(load_csv(path))
    if date_from:
        bars = [b for b in bars if b.start.strftime("%Y-%m-%d") >= date_from]
    if date_to:
        bars = [b for b in bars if b.start.strftime("%Y-%m-%d") <= date_to]
    return bars


@mcp.tool()
@_safe
def run_backtest(symbol: str, strategy: str | None = None, params: dict | None = None,
                 date_from: str | None = None, date_to: str | None = None, timeframe_min: int | None = None,
                 qty: int = 1, csv: str | None = None, include_daily: bool = False) -> dict:
    """Backtest a strategy on cached 1-min candles using the config's exits/risk/charges.
    Fill model is conservative (next-bar open + slippage, stop checked before target).
    `csv` may name a file in data/candles (see list_data)."""
    from bot.backtest import run_backtest as bt
    cfg = copy.deepcopy(_cfg())
    if timeframe_min:
        cfg["strategy"]["timeframe_min"] = timeframe_min
    out = control.RUNTIME_DIR / "backtests" / f"{symbol}_{strategy or cfg['strategy']['name']}.csv"
    res = bt(cfg, _bars(symbol, date_from, date_to, csv), qty=qty, strategy=strategy, params=params, out_csv=out)
    if not include_daily:
        res.pop("daily_pnl", None)
    return res


@mcp.tool()
@_safe
def param_sweep(symbol: str, strategy: str, grid: dict, date_from: str | None = None,
                date_to: str | None = None, csv: str | None = None, top: int = 10) -> dict:
    """Grid-search strategy params, e.g. grid={"fast":[5,9,13],"slow":[21,34]} (max 60 combos).
    Ranked by net P&L. WARNING: in-sample results overfit — always confirm the winner on a
    later, unseen date range (walk-forward) before changing config."""
    from bot.backtest import run_backtest as bt
    keys = list(grid)
    combos = list(itertools.product(*[grid[k] for k in keys]))
    if len(combos) > 60:
        raise ValueError(f"{len(combos)} combos > 60; narrow the grid")
    cfg = _cfg()
    bars = _bars(symbol, date_from, date_to, csv)
    rows = []
    for combo in combos:
        p = dict(zip(keys, combo))
        try:
            r = bt(copy.deepcopy(cfg), bars, strategy=strategy, params=p)
            rows.append({"params": p, **{k: r[k] for k in ("trades", "net_pnl", "win_rate_pct",
                                                            "profit_factor", "max_drawdown")}})
        except Exception as e:
            rows.append({"params": p, "error": str(e)})
    rows.sort(key=lambda r: r.get("net_pnl", float("-inf")), reverse=True)
    return {"combos": len(combos), "top": rows[:top],
            "warning": "In-sample ranking. Validate on out-of-sample dates before adopting."}


@mcp.tool()
@_safe
def fetch_candles(symbol: str, exchange: str = "NSE", days: int = 30) -> dict:
    """Download 1-min candles from SmartAPI into data/candles (logs in with .env credentials).
    Prefer running outside market hours."""
    from bot.broker import LiveBroker
    from bot.data import csv_path, fetch, save_csv
    from bot.instruments import load_master, resolve
    from bot.timeutil import now_ist
    from datetime import timedelta
    inst = resolve([{"symbol": symbol, "exchange": exchange, "qty": 1}], load_master())[0]
    b = LiveBroker(C.credentials())
    b.login()
    try:
        end = now_ist().replace(second=0, microsecond=0)
        rows = fetch(b, inst, (end - timedelta(days=days)).replace(hour=9, minute=15), end)
        save_csv(rows, csv_path(inst.symbol))
    finally:
        b.logout()
    return {"symbol": symbol, "rows": len(rows), "file": str(csv_path(inst.symbol))}


@mcp.tool()
@_safe
def search_instruments(query: str, exchange: str | None = None, limit: int = 20) -> dict:
    """Find Angel One trading symbols/tokens (e.g. 'SBIN', 'NIFTY' with exchange='NFO')."""
    from bot.instruments import load_master, search
    return {"results": search(load_master(), query, exchange, limit)}


# ───────────────────────────── control ─────────────────────────────
@mcp.tool()
def pause_bot(reason: str = "") -> dict:
    """Stop NEW entries. Open positions keep their stop-loss/target/square-off management."""
    return control.set_control(paused=True, note=f"claude pause: {reason}")


@mcp.tool()
def resume_bot() -> dict:
    """Allow new entries again (clears pause and any leftover kill flag)."""
    return control.set_control(paused=False, kill=False, note="claude resume")


@mcp.tool()
def kill_switch(confirm: str, reason: str = "") -> dict:
    """EMERGENCY: cancel open entries, exit ALL positions with limit orders, then stop the bot.
    Only use when the user explicitly asks. `confirm` must be exactly "FLATTEN"."""
    if confirm != "FLATTEN":
        return {"ok": False, "error": 'confirm must be exactly "FLATTEN"'}
    c = control.set_control(kill=True, note=f"claude kill: {reason}")
    return {"ok": True, "control": c,
            "note": "Bot will flatten within ~1s if running. Verify with bot_status and in the Angel One app."}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
