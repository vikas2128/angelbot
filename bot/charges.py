"""Angel One charges for NSE equity INTRADAY, per executed order.

Rates (config `charges:`), as published on https://www.angelone.in/exchange-transaction-charges (checked 23 Oct 2026):
  brokerage          lower of ₹20 or 0.1% of the order value, minimum ₹5 per executed order, and never more than the
                     exchange maximum (2.5% of the order value)
  exchange txn (NSE) 0.0030699% of value, buy and sell   (NSE circular FA73061, Feb 2026: the IPFT increase of
  IPFT (NSE)         0.0000001% of value, buy and sell    2023 was rolled back into the transaction charge)
  SEBI fee           ₹10 per crore of value, buy and sell
  STT                0.025% of value, SELL only, rounded to the nearest rupee (50 paise and above rounds up)
  stamp duty         0.003% of value, BUY only
  GST                18% of (brokerage + exchange txn + SEBI fee + IPFT)

Brokerage is per executed ORDER, so partial fills of one order share one brokerage (see OrderManager). Contract notes
round STT per stock per day; this rounds it per sell order, which differs by at most ₹0.50 when a stock is sold more
than once in a day. Other amounts are kept to the paisa. Not included: DP charges (none for intraday) and ₹20 Call &
Trade (orders by phone; never used by the bot).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

DEFAULTS = {
    "brokerage_flat": 20.0, "brokerage_pct": 0.1, "brokerage_min": 5.0, "brokerage_max_pct": 2.5,
    "exchange_txn_pct": 0.0030699, "ipft_pct": 0.0000001, "sebi_per_crore": 10.0,
    "stt_sell_pct": 0.025, "stt_round_rupee": True, "stamp_buy_pct": 0.003, "gst_pct": 18.0,
}


def _rate(c: dict, key: str) -> float:
    v = c.get(key, DEFAULTS[key])
    return float(DEFAULTS[key] if v is None else v)


def _rupee(x: float) -> float:
    return float(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def breakdown(side: str, value: float, c: dict | None = None) -> dict:
    """Each charge on one executed order of `value` rupees (shares x price), and the total."""
    c = c or {}
    if value <= 0:
        return {k: 0.0 for k in ("brokerage", "exchange_txn", "ipft", "sebi", "stt", "stamp", "gst", "total")}
    brokerage = min(_rate(c, "brokerage_flat"), value * _rate(c, "brokerage_pct") / 100)
    brokerage = max(brokerage, _rate(c, "brokerage_min"))
    brokerage = min(brokerage, value * _rate(c, "brokerage_max_pct") / 100)
    exch = value * _rate(c, "exchange_txn_pct") / 100
    ipft = value * _rate(c, "ipft_pct") / 100
    sebi = value * _rate(c, "sebi_per_crore") / 1e7
    stt = value * _rate(c, "stt_sell_pct") / 100 if side == "SELL" else 0.0
    if stt and c.get("stt_round_rupee", DEFAULTS["stt_round_rupee"]):
        stt = _rupee(stt)
    stamp = value * _rate(c, "stamp_buy_pct") / 100 if side == "BUY" else 0.0
    gst = (brokerage + exch + sebi + ipft) * _rate(c, "gst_pct") / 100
    parts = {"brokerage": brokerage, "exchange_txn": exch, "ipft": ipft, "sebi": sebi, "stt": stt, "stamp": stamp, "gst": gst}
    out = {k: round(v, 4) for k, v in parts.items()}
    out["total"] = round(sum(parts.values()), 2)
    return out


def value_charges(side: str, value: float, c: dict | None = None) -> float:
    return breakdown(side, value, c)["total"]


def order_charges(side: str, qty: int, price: float, c: dict | None = None) -> float:
    """Total charges on one executed order of `qty` shares at `price`."""
    return value_charges(side, qty * price, c)
