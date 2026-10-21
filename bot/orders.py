"""Order manager: places LIMIT orders, tracks fills, chases exits, keeps positions & P&L."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from bot import notify
from bot.broker import BrokerError
from bot.charges import order_charges
from bot.instruments import round_to_tick
from bot.journal import Journal
from bot.models import Instrument, OrderRecord, Position

log = logging.getLogger("angelbot.orders")


class OrderManager:
    def __init__(self, broker, journal: Journal, cfg: dict, instruments: list[Instrument],
                 strategy_name: str, on_trade_closed: Callable[[float, datetime], None]):
        self.broker = broker
        self.journal = journal
        self.cfg = cfg
        self.x = cfg["execution"]
        self.exits_cfg = cfg["exits"]
        self.mode = broker.mode
        self.inst = {i.symbol: i for i in instruments}
        self.positions: dict[str, Position] = {i.symbol: Position(i.symbol) for i in instruments}
        self.pending: dict[str, OrderRecord] = {}
        self.strategy_name = strategy_name
        self.on_trade_closed = on_trade_closed
        self.realized_net = 0.0          # day P&L after charges, closed trades
        self.charges_today = 0.0
        self._cycle: dict[str, dict] = {}  # per-symbol open trade bookkeeping
        self.last_poll: datetime | None = None
        self.exit_alerted: set[str] = set()

    # ------------------------------------------------------------------ queries
    def has_pending(self, symbol: str, purpose: str | None = None) -> bool:
        return any(o.symbol == symbol and (purpose is None or o.purpose == purpose) for o in self.pending.values())

    def open_positions(self) -> int:
        return sum(1 for p in self.positions.values() if p.qty != 0)

    def unrealized(self, ltps: dict[str, float]) -> float:
        return sum(p.unrealized(ltps[s]) for s, p in self.positions.items() if p.qty and s in ltps)

    # ------------------------------------------------------------------ actions
    def _limit_price(self, inst: Instrument, side: str, ltp: float, buffer_pct: float) -> float:
        if side == "BUY":
            return round_to_tick(ltp * (1 + buffer_pct / 100), inst.tick_size, "up")
        return round_to_tick(ltp * (1 - buffer_pct / 100), inst.tick_size, "down")

    def enter(self, symbol: str, direction: int, ltp: float, now: datetime, reason: str) -> str | None:
        inst = self.inst[symbol]
        side = "BUY" if direction > 0 else "SELL"
        price = self._limit_price(inst, side, ltp, float(self.x["entry_buffer_pct"]))
        return self._place(inst, side, inst.order_qty, price, "ENTRY", now, reason)

    def exit(self, symbol: str, ltp: float, now: datetime, reason: str) -> str | None:
        pos = self.positions[symbol]
        if pos.qty == 0 or self.has_pending(symbol, "EXIT"):
            return None
        # cancel any still-open entry for this symbol first
        for oid, o in list(self.pending.items()):
            if o.symbol == symbol and o.purpose == "ENTRY":
                self._safe_cancel(oid, now, "superseded by exit")
        inst = self.inst[symbol]
        side = "SELL" if pos.qty > 0 else "BUY"
        price = self._limit_price(inst, side, ltp, float(self.x["exit_buffer_pct"]))
        self._cycle.setdefault(symbol, {})["exit_reason"] = reason
        return self._place(inst, side, abs(pos.qty), price, "EXIT", now, reason)

    def _place(self, inst, side, qty, price, purpose, now, reason) -> str | None:
        try:
            oid = self.broker.place_limit(inst, side, qty, price)
        except BrokerError as e:
            log.error("order rejected by API: %s %s %s@%s — %s", side, qty, inst.symbol, price, e)
            self.journal.event(now, "order_error", str(e), "ERROR",
                               {"symbol": inst.symbol, "side": side, "qty": qty, "price": price})
            notify.send(f"❗ order error {side} {qty} {inst.symbol}: {e}")
            return None
        rec = OrderRecord(oid, inst.symbol, side, qty, price, purpose, now, reason=reason)
        self.pending[oid] = rec
        self.journal.order(rec, self.mode, now)
        log.info("%s %s %s %d @ %.2f (%s) id=%s", purpose, side, inst.symbol, qty, price, reason, oid)
        return oid

    def _safe_cancel(self, oid: str, now: datetime, why: str) -> None:
        try:
            self.broker.cancel(oid)
            log.info("cancel %s (%s)", oid, why)
        except BrokerError as e:
            log.warning("cancel %s failed: %s", oid, e)

    # ------------------------------------------------------------------ polling
    def poll(self, now: datetime, ltps: dict[str, float], force: bool = False) -> None:
        if not self.pending:
            return
        if not force and self.last_poll and (now - self.last_poll).total_seconds() < float(self.x["order_poll_sec"]):
            return
        self.last_poll = now
        try:
            st = self.broker.order_statuses(list(self.pending))
        except BrokerError as e:
            log.warning("order status poll failed: %s", e)
            return
        for oid, rec in list(self.pending.items()):
            s = st.get(oid)
            if s:
                self._apply_status(rec, s, now)
            if rec.done:
                self.pending.pop(oid, None)
                self.journal.order(rec, self.mode, now)
                continue
            age = (now - rec.placed_at).total_seconds()
            if rec.purpose == "ENTRY" and age > float(self.x["entry_timeout_sec"]):
                self._safe_cancel(oid, now, "entry timeout")
            elif rec.purpose == "EXIT" and age > float(self.x["exit_timeout_sec"]) * (rec.chases + 1):
                self._chase(rec, ltps.get(rec.symbol), now)

    def _chase(self, rec: OrderRecord, ltp: float | None, now: datetime) -> None:
        if ltp is None:
            return
        inst = self.inst[rec.symbol]
        rec.chases += 1
        widen = float(self.x["exit_buffer_pct"]) * min(rec.chases + 1, 5)
        price = self._limit_price(inst, rec.side, ltp, widen)
        remaining = rec.qty - rec.filled_qty
        try:
            self.broker.modify_price(rec.order_id, inst, rec.side, remaining if self.mode == "paper" else rec.qty, price)
            rec.price = price
            log.info("chase exit %s #%d → %.2f", rec.order_id, rec.chases, price)
        except BrokerError as e:
            log.warning("chase modify failed: %s", e)
        if rec.chases >= int(self.x["max_exit_chases"]) and rec.symbol not in self.exit_alerted:
            self.exit_alerted.add(rec.symbol)
            msg = f"⚠️ exit for {rec.symbol} not filled after {rec.chases} chases — CHECK MANUALLY"
            log.error(msg)
            self.journal.event(now, "exit_stuck", msg, "ERROR")
            notify.send(msg)

    def _apply_status(self, rec: OrderRecord, s: dict, now: datetime) -> None:
        new_filled = int(s.get("filled_qty") or 0)
        if s["status"] == "complete" and new_filled == 0:
            new_filled = rec.qty
        delta = new_filled - rec.filled_qty
        if delta > 0:
            px = float(s.get("avg_fill") or rec.price)
            rec.filled_qty, rec.avg_fill = new_filled, px
            self._apply_fill(rec, delta, px, now)
        if s["status"] != "open":
            rec.status = s["status"]
            if s["status"] == "rejected":
                rec.reason = s.get("reason", "")
                log.error("order %s REJECTED: %s", rec.order_id, rec.reason)
                self.journal.event(now, "order_rejected", rec.reason, "ERROR", {"order_id": rec.order_id})
                notify.send(f"❗ {rec.purpose} {rec.side} {rec.symbol} rejected: {rec.reason}")

    # ------------------------------------------------------------------ positions
    def _apply_fill(self, rec: OrderRecord, qty: int, px: float, now: datetime) -> None:
        pos = self.positions[rec.symbol]
        log.info("FILL %s %s %d @ %.2f (pos before %d)", rec.side, rec.symbol, qty, px, pos.qty)
        signed = qty if rec.side == "BUY" else -qty
        cyc = self._cycle.setdefault(rec.symbol, {})
        fee = order_charges(rec.side, qty, px, self.cfg.get("charges", {}))
        cyc["charges"] = cyc.get("charges", 0.0) + fee
        self.charges_today += fee

        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):     # open / add
            total = abs(pos.qty) + qty
            pos.avg_price = (pos.avg_price * abs(pos.qty) + px * qty) / total
            if pos.qty == 0:
                pos.entry_time = now
                cyc.update(side="LONG" if signed > 0 else "SHORT", entry_ts=now, gross=0.0, max_qty=0)
                notify.send(f"🟢 {cyc['side']} {rec.symbol} {qty} @ {px:.2f}")
            pos.qty += signed
            cyc["max_qty"] = max(cyc.get("max_qty", 0), abs(pos.qty))
            cyc["entry_price"] = pos.avg_price
            self._set_brackets(pos)
        else:                                                 # reduce / close
            closed = min(qty, abs(pos.qty))
            gross = (px - pos.avg_price) * closed * (1 if pos.qty > 0 else -1)
            pos.realized_pnl += gross
            cyc["gross"] = cyc.get("gross", 0.0) + gross
            pos.qty += signed if abs(signed) <= abs(pos.qty) else -pos.qty
            if pos.qty == 0:
                self._close_cycle(rec.symbol, px, now)

    def _set_brackets(self, pos: Position) -> None:
        sl, tgt = float(self.exits_cfg["stop_loss_pct"]) / 100, float(self.exits_cfg.get("target_pct") or 0) / 100
        if pos.qty > 0:
            pos.stop = pos.avg_price * (1 - sl)
            pos.target = pos.avg_price * (1 + tgt) if tgt else None
        else:
            pos.stop = pos.avg_price * (1 + sl)
            pos.target = pos.avg_price * (1 - tgt) if tgt else None
        pos.best_price = pos.avg_price

    def _close_cycle(self, symbol: str, exit_px: float, now: datetime) -> None:
        pos = self.positions[symbol]
        cyc = self._cycle.pop(symbol, {})
        gross, charges = cyc.get("gross", 0.0), cyc.get("charges", 0.0)
        net = gross - charges
        self.realized_net += net
        self.journal.trade(self.mode, symbol, cyc.get("side", "?"), cyc.get("max_qty", 0), cyc.get("entry_ts"),
                           cyc.get("entry_price", 0.0), now, exit_px, gross, charges,
                           cyc.get("exit_reason", "unknown"), self.strategy_name)
        notify.send(f"🔴 closed {symbol} net ₹{net:.2f} ({cyc.get('exit_reason', '')})")
        log.info("TRADE CLOSED %s gross=%.2f charges=%.2f net=%.2f", symbol, gross, charges, net)
        pos.avg_price, pos.stop, pos.target, pos.best_price, pos.entry_time = 0.0, None, None, None, None
        self.exit_alerted.discard(symbol)
        self.on_trade_closed(net, now)

    def adopt(self, symbol: str, netqty: int, avg: float, now: datetime) -> None:
        """Take over a position found at the broker on startup (e.g. after a crash/restart)."""
        pos = self.positions[symbol]
        pos.qty, pos.avg_price, pos.entry_time = netqty, avg, now
        self._cycle[symbol] = {"side": "LONG" if netqty > 0 else "SHORT", "entry_ts": now, "gross": 0.0,
                               "max_qty": abs(netqty), "entry_price": avg, "charges": 0.0}
        self._set_brackets(pos)
        log.warning("adopted existing position %s %d @ %.2f", symbol, netqty, avg)
