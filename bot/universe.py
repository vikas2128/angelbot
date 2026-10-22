"""Screener universe presets: the stocks in the Nifty 50 and Bank Nifty, from NSE's official constituent CSVs.

STOCKS ONLY (cash equity, series EQ). Index futures and options are not included: they are a different instrument type.

Index membership changes at NSE's reviews, so the lists are not typed in here. A snapshot of each official CSV ships in
config/universe/. `python -m bot.cli universe refresh` downloads fresh copies into data/universe/ (git-ignored, so a refresh
on the VM never conflicts with `git pull`); a refreshed copy wins over the shipped one. A refresh is validated first and
never replaces a good list with a broken one.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bot import config as C

SHIPPED_DIR = C.ROOT / "config" / "universe"
STALE_DAYS = 120          # NSE reviews its indices twice a year
HOSTS = ("https://nsearchives.nseindia.com/content/indices", "https://www.niftyindices.com/IndexConstituent")
PRESETS = {
    "nifty50": {"file": "nifty50.csv", "title": "Nifty 50", "remote": "ind_nifty50list.csv", "min": 40, "max": 60},
    "banknifty": {"file": "banknifty.csv", "title": "Bank Nifty", "remote": "ind_niftybanklist.csv", "min": 8, "max": 20},
}
ALIASES = {"niftybank": "banknifty", "nifty_bank": "banknifty", "nifty_50": "nifty50", "nifty-50": "nifty50"}
_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,20}$")


class UniverseError(Exception):
    pass


def refreshed_dir() -> Path:
    return C.DATA_DIR / "universe"


def canonical(name: str) -> str | None:
    n = name.strip().lower()
    n = ALIASES.get(n, n)
    return n if n in PRESETS else None


def parse(text: str, name: str) -> list[str]:
    """Plain NSE symbols (no -EQ) from an official CSV. Raises UniverseError if it does not look like the right list."""
    spec = PRESETS[name]
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not {"Symbol", "Series"} <= set(rows[0]):
        raise UniverseError(f"{spec['title']}: not a constituent list (no Symbol and Series columns)")
    out: list[str] = []
    for r in rows:
        if (r.get("Series") or "").strip() != "EQ":
            continue
        sym = (r.get("Symbol") or "").strip().upper()
        if not _SYMBOL.match(sym):
            raise UniverseError(f"{spec['title']}: unexpected symbol {sym!r}")
        if sym not in out:
            out.append(sym)
    if not spec["min"] <= len(out) <= spec["max"]:
        raise UniverseError(f"{spec['title']}: {len(out)} stocks, expected {spec['min']} to {spec['max']}; not replacing the current list")
    return out


def _find(name: str) -> Path | None:
    for d in (refreshed_dir(), SHIPPED_DIR):
        p = d / PRESETS[name]["file"]
        if p.exists():
            return p
    return None


def _meta(directory: Path) -> dict:
    try:
        return json.loads((directory / "meta.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load(name: str) -> tuple[list[str], dict]:
    """(trading symbols such as SBIN-EQ, info about the copy used). Raises UniverseError if there is no copy."""
    p = _find(name)
    if p is None:
        raise UniverseError(f"{PRESETS[name]['title']} list not found: run  python -m bot.cli universe refresh")
    syms = parse(p.read_text(encoding="utf-8"), name)
    info = {**_meta(p.parent).get(name, {}), "name": name, "title": PRESETS[name]["title"], "count": len(syms),
            "where": "refreshed" if p.parent == refreshed_dir() else "shipped"}
    return [f"{s}-EQ" for s in syms], info


def _age_days(fetched_at: str | None, now: datetime | None = None) -> int | None:
    try:
        return ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(fetched_at)).days
    except (TypeError, ValueError):
        return None


def expand(entries: list[str], now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Replace preset names (nifty50, banknifty) with their stocks. Returns (symbols without repeats, notes to show)."""
    out: list[str] = []
    notes: list[str] = []
    for e in entries:
        name = canonical(e)
        if name is None:
            syms = [e]
        else:
            try:
                syms, info = load(name)
            except UniverseError as err:
                notes.append(str(err))
                continue
            age = _age_days(info.get("fetched_at"), now)
            if age is not None and age > STALE_DAYS:
                notes.append(f"{info['title']} list is {age} days old; refresh it with  python -m bot.cli universe refresh")
        out.extend(s for s in syms if s not in out)
    return out, notes


def status(now: datetime | None = None) -> list[dict]:
    rows = []
    for name, spec in PRESETS.items():
        try:
            _, info = load(name)
        except UniverseError as e:
            rows.append({"name": name, "title": spec["title"], "error": str(e)})
            continue
        age = _age_days(info.get("fetched_at"), now)
        rows.append({**info, "age_days": age, "stale": age is not None and age > STALE_DAYS})
    return rows


def fetch_csv(url: str) -> str:
    """Download one CSV (a normal browser User-Agent: NSE refuses bare scripts)."""
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"})
    r.raise_for_status()
    return r.content.decode("utf-8-sig")


def refresh(names: list[str] | None = None, fetch=fetch_csv, now: datetime | None = None) -> list[dict]:
    """Download, validate and save fresh copies into data/universe/. A failure leaves the current list untouched."""
    now = now or datetime.now(timezone.utc)
    report = []
    for name in names or list(PRESETS):
        spec = PRESETS[name]
        try:
            old = set(load(name)[0])
        except UniverseError:
            old = set()
        errors, got = [], None
        for host in HOSTS:
            url = f"{host}/{spec['remote']}"
            try:
                text = fetch(url)
                got = (url, text, parse(text, name))
                break
            except Exception as e:                                # network error, or a page that is not the list
                errors.append(f"{url}: {e}")
        if got is None:
            report.append({"name": name, "title": spec["title"], "ok": False, "error": "; ".join(errors)})
            continue
        url, text, syms = got
        d = refreshed_dir()
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / spec["file"], text)
        meta = _meta(d)
        meta[name] = {"fetched_at": now.isoformat(timespec="seconds"), "source": url, "count": len(syms)}
        _atomic_write(d / "meta.json", json.dumps(meta, indent=2))
        new = {f"{s}-EQ" for s in syms}
        report.append({"name": name, "title": spec["title"], "ok": True, "count": len(syms), "source": url,
                       "added": sorted(new - old) if old else [], "removed": sorted(old - new)})
    return report


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
