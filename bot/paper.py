"""Paper broker: same interface as LiveBroker, fills simulated against live ticks.

A BUY limit fills when LTP <= limit price; a SELL limit fills when LTP >= limit.
Fill price = LTP ± slippage, capped at the limit price (never worse than the limit).
"""
from __future__ import annotations

import itertools
import threading
import uuid

from bot.models import Instrument


class PaperBroker:
    mode = "paper"

    def __init__(self, slippage_pct: float = 0.02):
        self.slip = slippage_pct / 100
        self.orders: dict[str, dict] = {}
        self.ltp_by_token: dict[str, float] = {}
        self.pos: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self.lock = threading.Lock()

    # ---- market data hook (engine calls this for every tick) ----
    def on_tick(self, token: str, ltp: float) -> None:
        with self.lock:
            self.ltp_by_token[token] = ltp
            for oid, o in self.orders.items():
                if o["status"] == "open" and o["inst"].token == token:
                    self._try_fill(o, ltp)

    def _try_fill(self, o: dict, ltp: float) -> None:
        if o["side"] == "BUY" and ltp <= o["price"]:
            px = min(o["price"], ltp * (1 + self.slip))
        elif o["side"] == "SELL" and ltp >= o["price"]:
            px = max(o["price"], ltp * (1 - self.slip))
        else:
            return
        o.update(status="complete", filled_qty=o["qty"], avg_fill=round(px, 2))
        sign = 1 if o["side"] == "BUY" else -1
        p = self.pos.setdefault(o["inst"].symbol, {"netqty": 0, "avg": 0.0, "token": o["inst"].token})
        new = p["netqty"] + sign * o["qty"]
        if p["netqty"] == 0 or (p["netqty"] > 0) == (sign > 0):
            p["avg"] = (p["avg"] * abs(p["netqty"]) + px * o["qty"]) / abs(new) if new else 0.0
        p["netqty"] = new
        if new == 0:
            p["avg"] = 0.0

    # ---- broker interface ----
    def login(self):
        return {"clientcode": "PAPER"}

    def place_limit(self, inst: Instrument, side: str, qty: int, price: float) -> str:
        oid = f"P{next(self._ids):04d}-{uuid.uuid4().hex[:8]}"
        with self.lock:
            self.orders[oid] = {"inst": inst, "side": side, "qty": qty, "price": price,
                                "status": "open", "filled_qty": 0, "avg_fill": 0.0}
            ltp = self.ltp_by_token.get(inst.token)
            if ltp is not None:   # marketable limits fill immediately, like the exchange
                self._try_fill(self.orders[oid], ltp)
        return oid

    def modify_price(self, order_id, inst, side, qty, price) -> None:
        with self.lock:
            o = self.orders[order_id]
            if o["status"] == "open":
                o["price"] = price
                ltp = self.ltp_by_token.get(inst.token)
                if ltp is not None:
                    self._try_fill(o, ltp)

    def cancel(self, order_id: str) -> None:
        with self.lock:
            o = self.orders.get(order_id)
            if o and o["status"] == "open":
                o["status"] = "cancelled"

    def order_statuses(self, order_ids):
        with self.lock:
            return {oid: {"status": self.orders[oid]["status"], "raw_status": self.orders[oid]["status"],
                          "filled_qty": self.orders[oid]["filled_qty"], "avg_fill": self.orders[oid]["avg_fill"],
                          "reason": ""} for oid in order_ids if oid in self.orders}

    def positions(self):
        return [{"symbol": s, "token": p["token"], "netqty": p["netqty"], "avg": p["avg"], "product": "PAPER"}
                for s, p in self.pos.items() if p["netqty"]]

    def funds(self):
        return {"note": "paper mode"}

    def logout(self):
        pass
