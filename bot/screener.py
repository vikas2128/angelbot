"""Stock screener: ranks a universe on liquidity, room to move versus costs, gap and volume.

ADVISORY ONLY. Nothing here places orders or changes what the bot trades: the bot still trades just the
instruments listed in config.yaml. The ranking is a heuristic that has NOT been backtested; treat it as a way
to shortlist stocks to test, not as a signal.

Pure logic (no network, no broker): raw per-stock data goes in, a ranked snapshot comes out.
"""
from __future__ import annotations

from datetime import datetime

from bot.charges import order_charges

# Liquid NSE large caps to start from. An editable starting list, not a ranking. Symbols that do not
# exist in the instrument master are reported as errors and skipped.
DEFAULT_UNIVERSE = [
    "SBIN-EQ", "RELIANCE-EQ", "HDFCBANK-EQ", "ICICIBANK-EQ", "INFY-EQ", "TCS-EQ", "ITC-EQ", "AXISBANK-EQ",
    "KOTAKBANK-EQ", "LT-EQ", "BHARTIARTL-EQ", "TATASTEEL-EQ", "HINDUNILVR-EQ", "MARUTI-EQ", "SUNPHARMA-EQ",
    "BAJFINANCE-EQ", "ONGC-EQ", "NTPC-EQ", "COALINDIA-EQ", "WIPRO-EQ",
]

DEFAULTS = {
    "order_value": 40000,          # planned ₹ per order: fees as a share of the order depend on this
    "min_traded_value_cr": 100.0,  # gate: average daily traded value, ₹ crore
    "max_spread_pct": 0.10,        # gate: best bid-ask spread, % of price (only if the quote has depth)
    "min_range_to_cost": 5.0,      # gate: average daily range must be at least this many times round-trip costs
    "min_history_days": 10,        # gate: completed daily candles needed
    "lookback_days": 20,           # days used for the averages
    "top_n": 3,
    "gap_cap_pct": 3.0,            # |gap| above this counts the same
    "weights": {"range_to_cost": 0.35, "gap": 0.25, "volume": 0.25, "liquidity": 0.15},
}


def params_from(cfg: dict | None) -> dict:
    """Defaults, overridden by an optional `screener:` section of config.yaml."""
    p = {**DEFAULTS, "weights": dict(DEFAULTS["weights"])}
    over = dict((cfg or {}).get("screener") or {})
    w = over.pop("weights", None)
    p.update({k: v for k, v in over.items() if k in DEFAULTS and k != "weights"})
    if w:
        p["weights"].update({k: float(v) for k, v in w.items() if k in DEFAULTS["weights"]})
    return p


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def percentiles(vals: dict[str, float]) -> dict[str, float]:
    """Rank each value 0..1 within the group (ties share the average rank)."""
    items = sorted(vals.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 1:
        return {items[0][0]: 1.0}
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[items[k][0]] = ((i + j) / 2) / (n - 1)
        i = j + 1
    return out


def metrics(raw: dict, p: dict, charges_cfg: dict) -> dict:
    """Numbers for one stock from its raw data. Missing inputs give None, never a guess."""
    daily = sorted(raw.get("daily") or [], key=lambda d: d["d"])[-int(p["lookback_days"]):]
    last_close = daily[-1]["c"] if daily else 0.0
    ltp = float(raw.get("ltp") or 0) or last_close
    prev_close = float(raw.get("prev_close") or 0) or last_close
    opn = float(raw.get("open") or 0)
    bid, ask = float(raw.get("bid") or 0), float(raw.get("ask") or 0)
    avg_vol = _mean([d["v"] for d in daily])
    m: dict = {"symbol": raw["symbol"], "token": raw.get("token"), "ltp": round(ltp, 2) if ltp else None,
               "prev_close": round(prev_close, 2) if prev_close else None, "days": len(daily),
               "traded_value_cr": round(_mean([d["c"] * d["v"] for d in daily]) / 1e7, 1) if daily else None,
               "range_pct": round(_mean([(d["h"] - d["l"]) / d["c"] * 100 for d in daily if d["c"]]), 2) if daily else None}
    trs = [max(d["h"] - d["l"], abs(d["h"] - daily[i]["c"]), abs(d["l"] - daily[i]["c"])) for i, d in enumerate(daily[1:])]
    m["atr_pct"] = round(_mean(trs[-14:]) / last_close * 100, 2) if trs and last_close else None
    m["gap_pct"] = round((opn - prev_close) / prev_close * 100, 2) if opn > 0 and prev_close > 0 else None
    m["spread_pct"] = round((ask - bid) / ((ask + bid) / 2) * 100, 3) if bid > 0 and ask >= bid > 0 else None
    vol = raw.get("volume")
    m["vol_pct_of_avg"] = round(float(vol) / avg_vol * 100, 1) if vol is not None and avg_vol > 0 else None
    if ltp > 0:
        qty = max(1, int(float(p["order_value"]) // ltp))
        cost = order_charges("BUY", qty, ltp, charges_cfg) + order_charges("SELL", qty, ltp, charges_cfg)
        m["suggested_qty"] = qty
        m["round_trip_cost_pct"] = round(cost / (qty * ltp) * 100, 3)
        m["range_to_cost"] = round(m["range_pct"] / m["round_trip_cost_pct"], 1) if m["range_pct"] else None
    else:
        m.update(suggested_qty=None, round_trip_cost_pct=None, range_to_cost=None)
    return m


def gates(m: dict, p: dict) -> list[str]:
    """Reasons a stock is excluded (empty list: eligible)."""
    out = []
    if m["days"] < int(p["min_history_days"]):
        out.append(f"only {m['days']} days of history (need {p['min_history_days']})")
        return out
    if m["ltp"] is None:
        out.append("no price")
    if m["traded_value_cr"] is not None and m["traded_value_cr"] < float(p["min_traded_value_cr"]):
        out.append(f"traded value ₹{m['traded_value_cr']:.0f} cr/day is below ₹{float(p['min_traded_value_cr']):.0f} cr")
    if m["spread_pct"] is not None and m["spread_pct"] > float(p["max_spread_pct"]):
        out.append(f"spread {m['spread_pct']:.2f}% is above {float(p['max_spread_pct']):.2f}%")
    if m["range_to_cost"] is not None and m["range_to_cost"] < float(p["min_range_to_cost"]):
        out.append(f"daily range is only {m['range_to_cost']:.1f}x costs (need {float(p['min_range_to_cost']):.0f}x)")
    return out


def rank(raws: list[dict], p: dict, charges_cfg: dict, now: datetime, errors: list[str] | None = None) -> dict:
    """Score the eligible stocks 0-100 and return the snapshot (rows in rank order, then the excluded)."""
    rows = []
    for raw in raws:
        m = metrics(raw, p, charges_cfg)
        m["excluded"] = gates(m, p)
        rows.append(m)
    ok = [r for r in rows if not r["excluded"]]
    cap = float(p["gap_cap_pct"])
    factors = {
        "range_to_cost": {r["symbol"]: r["range_to_cost"] for r in ok if r["range_to_cost"] is not None},
        "gap": {r["symbol"]: min(abs(r["gap_pct"]), cap) for r in ok if r["gap_pct"] is not None},
        "volume": {r["symbol"]: r["vol_pct_of_avg"] for r in ok if r["vol_pct_of_avg"] is not None},
        "liquidity": {r["symbol"]: r["traded_value_cr"] for r in ok if r["traded_value_cr"] is not None},
    }
    pct = {f: percentiles(v) if v else {} for f, v in factors.items()}
    words = {"range_to_cost": lambda r: f"room to move {r['range_to_cost']:.1f}x costs",
             "gap": lambda r: f"gap {r['gap_pct']:+.1f}%",
             "volume": lambda r: f"{r['vol_pct_of_avg']:.0f}% of a normal day's volume traded so far",
             "liquidity": lambda r: f"₹{r['traded_value_cr']:.0f} cr/day traded"}
    for r in ok:
        parts = [(f, float(p["weights"][f]), pct[f][r["symbol"]]) for f in factors if r["symbol"] in pct[f]]
        wsum = sum(w for _, w, _ in parts)
        r["score"] = round(sum(w * x for _, w, x in parts) / wsum * 100, 1) if wsum else 0.0
        top = sorted(parts, key=lambda t: t[1] * t[2], reverse=True)[:2]
        r["why"] = [words[f](r) for f, _, _ in top]
        if r["gap_pct"] is None:
            r["why"].append("no gap data yet")
    ok.sort(key=lambda r: (-r["score"], -(r["traded_value_cr"] or 0)))
    for i, r in enumerate(ok, 1):
        r["rank"] = i
    bad = sorted([r for r in rows if r["excluded"]], key=lambda r: r["symbol"])
    for r in bad:
        r.update(score=None, rank=None, why=[])
    return {"generated_at": now.isoformat(timespec="seconds"), "market_date": now.strftime("%Y-%m-%d"),
            "params": p, "universe": len(raws), "eligible": len(ok), "picks": [r["symbol"] for r in ok[: int(p["top_n"])]],
            "rows": ok + bad, "errors": list(errors or [])}
