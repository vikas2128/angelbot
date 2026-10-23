"""Settings changed from Telegram: a small, fixed set of config values with fixed choices, kept per area.

Changes are stored in runtime/<area>/settings.yaml (git-ignored), NOT in config/config.yaml: load() reads
config.yaml and lays these values on top (the bot, the CLI and the MCP server load their config through it). So a change in the Paper bot never touches research or live, and a
`git pull` never conflicts. Every change is validated on the merged config before it is saved; an invalid one is
refused and nothing is written.

Deliberately NOT here: paper/live mode, orders per second, order type, the order cap, session times.
"""
from __future__ import annotations

import copy
import os
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from bot import config as C

HISTORY_KEEP = 20
_BUILTIN = "builtin"

# id (short, for button data), label, dotted config path, choices. "instruments.qty" sets every instrument's qty.
SETTINGS = [
    {"id": "selmode", "btn": "Mode", "label": "Selection mode", "path": "selection.mode", "choices": ["off", "auto", "confirm"]},
    {"id": "topn", "btn": "Picks", "label": "Picks to trade", "path": "selection.top_n", "choices": [1, 2, 3]},
    {"id": "selqty", "btn": "Pick shares", "label": "Shares for a picked stock", "path": "selection.qty", "choices": [1, 2, 5, 10, 20, 50]},
    {"id": "listqty", "btn": "List shares", "label": "Shares for my list", "path": "instruments.qty", "choices": [1, 2, 5, 10, 20, 50]},
    {"id": "univ", "btn": "Universe", "label": "Stocks to screen", "path": "screener.universe",
     "choices": [_BUILTIN, ["nifty50"], ["banknifty"], ["nifty50", "banknifty"]]},
    {"id": "strat", "btn": "Strategy", "label": "Strategy", "path": "strategy.name", "choices": ["ema_cross", "orb", "vwap_revert"]},
    {"id": "maxloss", "btn": "Max loss", "label": "Max daily loss", "path": "risk.max_daily_loss", "choices": [250, 500, 1000, 2000, 5000]},
    {"id": "maxtr", "btn": "Max trades", "label": "Max trades per day", "path": "risk.max_trades_per_day", "choices": [3, 5, 10, 20]},
    {"id": "sl", "btn": "Stop loss", "label": "Stop loss", "path": "exits.stop_loss_pct", "choices": [0.2, 0.3, 0.5, 0.75, 1.0]},
    {"id": "tgt", "btn": "Target", "label": "Target", "path": "exits.target_pct", "choices": [0.3, 0.6, 0.9, 1.2, 2.0]},
]
BY_ID = {s["id"]: s for s in SETTINGS}
_UNIVERSE_WORDS = {"nifty50": "Nifty 50", "banknifty": "Bank Nifty"}


class SettingsError(ValueError):
    pass


def path() -> Path:
    return C.RUNTIME_DIR / "settings.yaml"


def read() -> dict:
    try:
        d = yaml.safe_load(path().read_text()) or {}
    except FileNotFoundError:
        return {"values": {}, "history": []}
    return {"values": dict(d.get("values") or {}), "history": list(d.get("history") or [])}


def overrides() -> dict:
    return read()["values"]


def _get(cfg: dict, dotted: str):
    if dotted == "instruments.qty":
        qtys = {int(i.get("qty", 0)) for i in cfg.get("instruments") or []}
        return qtys.pop() if len(qtys) == 1 else None
    node = cfg
    for k in dotted.split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _set(cfg: dict, dotted: str, value) -> None:
    if dotted == "instruments.qty":
        for i in cfg.get("instruments") or []:
            i["qty"] = int(value)
        return
    if dotted == "screener.universe" and value == _BUILTIN:
        from bot.screener import DEFAULT_UNIVERSE
        value = list(DEFAULT_UNIVERSE)
    *parents, last = dotted.split(".")
    node = cfg
    for k in parents:
        if not isinstance(node.get(k), dict):
            node[k] = {}
        node = node[k]
    node[last] = copy.deepcopy(value)


def merge(cfg: dict, values: dict) -> dict:
    """config.yaml values with the Telegram overrides on top (unknown keys are ignored)."""
    out = copy.deepcopy(cfg)
    known = {s["path"] for s in SETTINGS}
    for k, v in (values or {}).items():
        if k in known:
            _set(out, k, v)
    return out


def load(config_path: Path | None = None) -> dict:
    """The config the bot runs with: config.yaml plus this area's Telegram overrides, validated."""
    cfg = merge(C.load_config(config_path), overrides())
    C.validate_config(cfg)
    return cfg


def current(cfg_merged: dict, s: dict):
    v = _get(cfg_merged, s["path"])
    if s["path"] == "screener.universe":
        from bot.screener import DEFAULT_UNIVERSE
        if v is None or list(v) == list(DEFAULT_UNIVERSE):
            return _BUILTIN
        return list(v)
    return v


def show(s: dict, v) -> str:
    """A value as the user reads it."""
    if v is None:
        return "not set"
    p = s["path"]
    if p == "screener.universe":
        if v == _BUILTIN:
            return "built-in 20 stocks"
        return " + ".join(_UNIVERSE_WORDS.get(x, str(x)) for x in v)
    if p == "risk.max_daily_loss":
        return f"₹{float(v):,.0f}"
    if p.startswith("exits."):
        return f"{float(v):g}%"
    if p.endswith(".qty"):
        return f"{int(v)} share{'s' if int(v) != 1 else ''}"
    return str(v)


def _validate(merged: dict) -> None:
    from bot import selection
    C.validate_config(merged)
    selection.settings(merged)


def _write(d: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp_")
    with os.fdopen(fd, "w") as f:
        f.write("# Settings changed from Telegram. They override config/config.yaml for this area only.\n")
        yaml.safe_dump(d, f, sort_keys=False, allow_unicode=True)
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def apply(sid: str, index: int, base: dict | None = None, now: datetime | None = None) -> tuple[str, str]:
    """Set one setting to one of its choices. Returns (old shown, new shown). Raises SettingsError, writes nothing, on a bad value."""
    s = BY_ID.get(sid)
    if s is None:
        raise SettingsError("Unknown setting.")
    if not 0 <= index < len(s["choices"]):
        raise SettingsError("Unknown choice.")
    base = base if base is not None else C.load_config()
    d = read()
    old = current(merge(base, d["values"]), s)
    value = s["choices"][index]
    values = dict(d["values"])
    if current(base, s) == value:
        values.pop(s["path"], None)                    # back to the config.yaml value: no override needed
    else:
        values[s["path"]] = value
    try:
        _validate(merge(base, values))
    except ValueError as e:
        raise SettingsError(f"Not saved: {e}") from None
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    hist = (d["history"] + [{"at": stamp, "setting": s["path"], "from": old, "to": value}])[-HISTORY_KEEP:]
    _write({"values": values, "history": hist})
    return show(s, old), show(s, value)


def reset(now: datetime | None = None) -> int:
    """Drop every Telegram override (back to config.yaml). Returns how many there were."""
    d = read()
    n = len(d["values"])
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    _write({"values": {}, "history": (d["history"] + [{"at": stamp, "setting": "all", "from": "telegram", "to": "config.yaml"}])[-HISTORY_KEEP:]})
    return n


def summary(values: dict | None = None) -> list[str]:
    """One line per override in use, e.g. 'Shares for a picked stock: 5 shares'."""
    values = overrides() if values is None else values
    by_path = {s["path"]: s for s in SETTINGS}
    return [f"{by_path[k]['label']}: {show(by_path[k], v)}" for k, v in values.items() if k in by_path]
