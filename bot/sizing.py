"""Position size from a rupee amount per trade (config `sizing.amount`).

With `sizing.amount: 0` (the default, also when the section is absent) every order uses the configured share count
(instruments[].qty, or selection.qty for a picked stock). With an amount > 0 the bot works out the shares at each
entry from the price: as many whole shares (or lots) as fit, so one order never costs more than the amount. If not
even one share fits, it does not enter.
"""
from __future__ import annotations

import math


def amount(cfg: dict) -> float:
    return float((cfg.get("sizing") or {}).get("amount") or 0)


def qty_for(amount_rs: float, price: float, lot_size: int = 1) -> int:
    """Whole shares (a multiple of the lot size) whose cost at `price` is at most `amount_rs`; 0 if none fit."""
    lot = max(1, int(lot_size))
    if not (math.isfinite(amount_rs) and math.isfinite(price)) or amount_rs <= 0 or price <= 0:
        return 0
    return int(amount_rs // (price * lot)) * lot


def validate(cfg: dict) -> None:
    """Raise ValueError if the amount is unusable."""
    a = amount(cfg)
    if not math.isfinite(a):
        raise ValueError("sizing.amount must be a number")
    cap = float((cfg.get("risk") or {}).get("max_order_value") or 0)
    if a < 0:
        raise ValueError("sizing.amount cannot be negative")
    if a and cap and a > cap:
        raise ValueError(f"sizing.amount ₹{a:,.0f} is above risk.max_order_value ₹{cap:,.0f}")
    if 0 < a < 100:
        raise ValueError("sizing.amount must be 0 (off) or at least ₹100")
