"""Morning stock selection: choose which stocks the bot trades today, from the screener's ranking.

OFF BY DEFAULT. With `selection.mode: off` (or no `selection:` section) the bot trades exactly the
instruments in config.yaml, as before. Nothing here places orders, and every trade still goes through the
same strategy, risk rules, stops and limits; this only decides WHICH instruments the engine is given at start.

Modes (config.yaml `selection:`):
  auto     screen at `screen_at`, trade the top `top_n` picks
  confirm  screen, send the picks to Telegram with buttons, wait until `confirm_until` for your tap;
           no tap: `on_timeout` (config = your configured list, top = the top picks)

Safety rules:
  * Only stocks that passed the screener's gates in today's snapshot can be chosen. A Telegram tap cannot add
    an arbitrary symbol.
  * The decision is saved for the day. After a restart the bot keeps the same stocks (so it can manage what it holds).
  * Any failure (screen, quotes, bad config) falls back per `on_failure`: config = your configured list, none = no trading.
  * The selection runs on the bot's own broker login: no second login.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from bot import control, messages, notify
from bot.models import Instrument
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.selection")

DEFAULTS = {
    "mode": "off",              # off | auto | confirm
    "top_n": 1,                 # how many of the top picks to trade
    "screen_at": "09:16:30",    # after the open, so today's gap exists
    "confirm_until": "09:19:30",  # must be at or before session.entry_start
    "on_timeout": "config",     # confirm mode, no tap: config | top
    "on_failure": "config",     # screen failed or nothing passed: config | none
    "qty": 1,                   # shares per order for a picked stock ...
    "order_value": 0,           # ... or, if > 0, shares = this many rupees // price (bigger size = bigger risk per trade)
    "max_symbols": 3,
}


def _hms(day: datetime, s: str) -> datetime:
    parts = [int(x) for x in str(s).split(":")] + [0, 0]
    return day.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)


def settings(cfg: dict) -> dict:
    """Defaults + the `selection:` section, validated. Raises ValueError with a readable message."""
    s = {**DEFAULTS, **{k: v for k, v in (cfg.get("selection") or {}).items() if k in DEFAULTS}}
    errs = []
    if s["mode"] not in ("off", "auto", "confirm"):
        errs.append("selection.mode must be off, auto or confirm")
    if s["on_timeout"] not in ("config", "top"):
        errs.append("selection.on_timeout must be config or top")
    if s["on_failure"] not in ("config", "none"):
        errs.append("selection.on_failure must be config or none")
    if not 1 <= int(s["top_n"]) <= int(s["max_symbols"]):
        errs.append(f"selection.top_n must be between 1 and {s['max_symbols']}")
    if int(s["qty"]) < 1 or float(s["order_value"]) < 0:
        errs.append("selection.qty must be at least 1 and selection.order_value cannot be negative")
    try:
        probe = datetime(2000, 1, 3)
        a, b = _hms(probe, s["screen_at"]), _hms(probe, s["confirm_until"])
        entry = _hms(probe, (cfg.get("session") or {}).get("entry_start", "09:20"))
        if not a < b <= entry:
            errs.append("selection times must satisfy screen_at < confirm_until <= session.entry_start")
    except (ValueError, TypeError, IndexError):
        errs.append("selection.screen_at and selection.confirm_until must look like 09:16:30")
    if errs:
        raise ValueError("; ".join(errs))
    return s


def _instrument(row: dict, s: dict, master: list[dict]) -> Instrument:
    from bot.instruments import resolve
    if float(s["order_value"]) > 0 and row.get("ltp"):
        qty = max(1, int(float(s["order_value"]) // float(row["ltp"])))
    else:
        qty = int(s["qty"])
    return resolve([{"symbol": row["symbol"], "exchange": "NSE", "qty": qty, "product": "INTRADAY"}], master)[0]


def _decide(day: str, symbols: list[str], source: str, note: str) -> dict:
    d = {"market_date": day, "symbols": symbols, "source": source, "note": note,
         "decided_at": now_ist().isoformat(timespec="seconds")}
    control.write_json(control.SEL_STATE, d)
    req = control.read_json(control.SEL_REQUEST)
    if req.get("market_date") == day:
        control.write_json(control.SEL_REQUEST, {**req, "phase": "decided", "decided": symbols})
    return d


def _wait_until(target: datetime, now_fn, sleep_fn, poll: float = 2.0) -> None:
    while now_fn() < target:
        sleep_fn(max(0.05, min(poll, (target - now_fn()).total_seconds())))


def _valid_choice(day: str, picks: list[str]) -> list[str] | str | None:
    """What you tapped, if it is for today and is one of today's picks (or 'config')."""
    c = control.read_json(control.SEL_CHOICE)
    if c.get("market_date") != day:
        return None
    ch = c.get("choice")
    if ch == "config":
        return "config"
    if isinstance(ch, list) and ch and all(x in picks for x in ch):
        return list(ch)
    return None


def run(cfg: dict, broker, config_insts: list[Instrument], master: list[dict], now_fn=now_ist,
        sleep_fn=time.sleep, notify_fn=notify.send, screen_fn=None) -> tuple[list[Instrument], dict]:
    """Return (instruments the engine should trade today, decision). Never raises: failures fall back."""
    try:
        s = settings(cfg)
    except ValueError as e:
        log.error("selection config invalid, using the configured list: %s", e)
        notify_fn(messages.selection_problem(str(e)))
        return config_insts, {"source": "config", "symbols": [i.symbol for i in config_insts], "note": "invalid selection config"}
    configured = [i.symbol for i in config_insts]
    if s["mode"] == "off":
        return config_insts, {"source": "off", "symbols": configured}
    day = now_fn().strftime("%Y-%m-%d")

    def finish(symbols: list[str], source: str, note: str, rows: dict | None = None):
        insts = []
        for sym in symbols:
            if sym in configured:
                insts.append(next(i for i in config_insts if i.symbol == sym))
            else:
                insts.append(_instrument((rows or {}).get(sym) or {"symbol": sym}, s, master))
        d = _decide(day, symbols, source, note)
        notify_fn(messages.selection_decided(symbols, source, note))
        return insts, d

    def fallback(why: str):
        log.warning("selection fallback (%s)", why)
        if s["on_failure"] == "none":
            d = _decide(day, [], "none", why)
            notify_fn(messages.selection_decided([], "none", why))
            return [], d
        return finish(configured, "config", why)

    try:
        prior = control.read_json(control.SEL_STATE)
        if prior.get("market_date") == day and prior.get("symbols") is not None:      # restart: keep today's stocks
            syms = prior["symbols"]
            log.info("reusing today's selection after a restart: %s", syms)
            if not syms:
                return [], prior
            rows = {r["symbol"]: r for r in control.read_screen().get("rows", [])}
            return [next((i for i in config_insts if i.symbol == x), None) or _instrument(rows.get(x) or {"symbol": x}, s, master)
                    for x in syms], prior
        _wait_until(_hms(now_fn(), s["screen_at"]), now_fn, sleep_fn)
        snap = (screen_fn or _screen)(cfg, broker, master, now_fn(), s)
        control.write_screen(snap)
        eligible = [r for r in snap["rows"] if not r["excluded"]]
        rows = {r["symbol"]: r for r in eligible}
        if not eligible:
            return fallback("no stock passed the screener's gates today")
        top = [r["symbol"] for r in eligible[: int(s["top_n"])]]
        shown = [r["symbol"] for r in eligible[: max(int(s["top_n"]), int((snap.get("params") or {}).get("top_n", 3)))]]
        if s["mode"] == "auto":
            return finish(top, "auto", "top picks of today's screen", rows)
        default = (top, "timeout", "no tap, so the top picks") if s["on_timeout"] == "top" else (configured, "timeout", "no tap, so your configured list")
        deadline = _hms(now_fn(), s["confirm_until"])
        control.write_json(control.SEL_REQUEST, {"market_date": day, "phase": "waiting", "picks": [
            {k: r.get(k) for k in ("symbol", "score", "gap_pct", "range_to_cost", "ltp", "why")} for r in eligible[: len(shown)]],
            "top": top, "deadline": deadline.isoformat(timespec="seconds"), "default": default[0]})
        notify_fn(messages.picks_prompt(eligible[: len(shown)], top, deadline.strftime("%H:%M"), default[0]),
                  messages.picks_buttons(shown, top, configured))
        while now_fn() < deadline:
            ch = _valid_choice(day, shown)
            if ch is not None:
                if ch == "config":
                    return finish(configured, "you", "you kept your configured list")
                return finish(ch, "you", "your choice", rows)
            sleep_fn(2.0)
        ch = _valid_choice(day, shown)                      # a tap in the last moment still counts
        if ch is not None:
            return finish(configured, "you", "you kept your configured list") if ch == "config" else finish(ch, "you", "your choice", rows)
        return finish(default[0], default[1], default[2], rows)
    except Exception as e:                                  # selection must never stop the bot from trading
        log.exception("selection failed")
        return fallback(f"selection failed: {e}")


def _screen(cfg: dict, broker, master: list[dict], now: datetime, s: dict) -> dict:
    from bot import screen_data
    return screen_data.run_screen(cfg, broker, master, now)
