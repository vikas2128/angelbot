"""Angel One instrument master (symbol ↔ token, lot size, tick size)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from bot.config import DATA_DIR
from bot.models import Instrument

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE = DATA_DIR / "scrip_master.json"


def load_master(path: Path | None = None, max_age_hours: float = 20) -> list[dict]:
    """Return the instrument master, downloading if the cache is missing/stale."""
    path = path or CACHE
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_hours * 3600:
        return json.loads(path.read_text())
    if path != CACHE and path.exists():   # explicit fixture path: never download
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(MASTER_URL, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    return r.json()


def _row_to_instrument(row: dict, qty: int = 1, product: str = "INTRADAY") -> Instrument:
    tick = float(row.get("tick_size") or 5) / 100  # master stores tick in paise
    return Instrument(
        symbol=row["symbol"],
        token=str(row["token"]),
        exchange=row["exch_seg"],
        lot_size=max(int(float(row.get("lotsize") or 1)), 1),
        tick_size=tick if tick > 0 else 0.05,
        qty=int(qty),
        product=product,
    )


def resolve(cfg_instruments: list[dict], master: list[dict]) -> list[Instrument]:
    idx = {(r["exch_seg"], r["symbol"]): r for r in master}
    out = []
    for ci in cfg_instruments:
        row = idx.get((ci["exchange"], ci["symbol"]))
        if row is None:
            raise ValueError(
                f"Instrument {ci['exchange']}:{ci['symbol']} not found in master. "
                f"Search with: python -m bot.cli instruments <name>"
            )
        out.append(_row_to_instrument(row, ci.get("qty", 1), ci.get("product", "INTRADAY")))
    return out


def search(master: list[dict], query: str, exchange: str | None = None, limit: int = 25) -> list[dict]:
    q = query.upper()
    res = [
        r for r in master
        if (q in r["symbol"].upper() or q == r.get("name", "").upper())
        and (exchange is None or r["exch_seg"] == exchange)
    ]
    res.sort(key=lambda r: (r["symbol"].upper() != q, len(r["symbol"])))
    return [
        {k: r.get(k) for k in ("symbol", "token", "exch_seg", "name", "expiry", "lotsize", "instrumenttype")}
        for r in res[:limit]
    ]


def round_to_tick(price: float, tick: float, direction: str = "nearest") -> float:
    """Round price to a valid tick. direction: up | down | nearest."""
    import math

    n = price / tick
    n = math.ceil(n - 1e-9) if direction == "up" else math.floor(n + 1e-9) if direction == "down" else round(n)
    return round(n * tick, 2)
