"""Approximate Indian intraday charges. Verify against Angel One's current tariff."""
from __future__ import annotations


def order_charges(side: str, qty: int, price: float, c: dict) -> float:
    turnover = qty * price
    brokerage = min(float(c.get("brokerage_flat", 20)), turnover * float(c.get("brokerage_pct", 0.1)) / 100)
    exch = turnover * float(c.get("exchange_txn_pct", 0.00297)) / 100
    sebi = turnover * float(c.get("sebi_per_crore", 10)) / 1e7
    stt = turnover * float(c.get("stt_sell_pct", 0.025)) / 100 if side == "SELL" else 0.0
    stamp = turnover * float(c.get("stamp_buy_pct", 0.003)) / 100 if side == "BUY" else 0.0
    gst = (brokerage + exch + sebi) * float(c.get("gst_pct", 18)) / 100
    return round(brokerage + exch + sebi + stt + stamp + gst, 2)
