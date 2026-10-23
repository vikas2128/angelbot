"""Main event loop: ticks → bars → strategy → risk → orders, plus stops, limits and control."""
from __future__ import annotations

import logging
import os
import queue
import time
from collections import deque
from datetime import datetime, timedelta

from bot import messages, notify, sizing
from bot.bars import BarBuilder
from bot.control import read_control, set_control, write_status
from bot.journal import Journal, trades_on
from bot.models import Bar, Instrument, Tick
from bot.orders import OrderManager
from bot.risk import RiskManager
from bot.strategies import build
from bot.timeutil import at, now_ist

log = logging.getLogger("angelbot.engine")


class Engine:
    def __init__(self, cfg: dict, broker, feed, instruments: list[Instrument], journal: Journal,
                 q: queue.Queue, replay: bool = False):
        self.cfg = cfg
        self.broker = broker
        self.feed = feed
        self.q = q
        self.replay = replay
        self.journal = journal
        self.instruments = instruments
        self.by_token = {i.token: i for i in instruments}
        st = cfg["strategy"]
        self.strategy_name = st["name"]
        self.allow_short = bool(st.get("allow_short"))
        params = (st.get("params") or {}).get(st["name"], {})
        self.strategies = {i.symbol: build(st["name"], params) for i in instruments}
        for s in self.strategies.values():
            s.reset_day()
        self.risk = RiskManager(cfg)
        self.om = OrderManager(broker, journal, cfg, instruments, st["name"], self._on_trade_closed)
        self.bars_today: dict[str, list[Bar]] = {i.symbol: [] for i in instruments}
        self.builder = BarBuilder(int(st.get("timeframe_min", 1)), self._on_bar)
        self.ltp: dict[str, float] = {}
        self.pending_reverse: dict[str, int] = {}
        self.recent_signals: deque = deque(maxlen=15)
        self.state = "starting"
        self.flatten_reason: str | None = None
        self._now: datetime | None = None
        self._last_status = 0.0
        self._last_control = 0.0
        self._last_feed_restart = 0.0
        self._feed_wait = 0.0              # seconds before the next feed restart may happen (backs off while it fails)
        self.control = {"paused": False, "kill": False}
        self.exit_code = 0
        self._feed_alerted = False       # notification state only
        self._heartbeat_sent = False
        self.restored: dict | None = None
        if not replay:
            self.restore_day()

    def restore_day(self) -> None:
        """After a restart, rebuild today's trade count, realised P&L, fees, loss streak and cooldown from the journal,
        so the daily limits carry on from where they were instead of starting again from zero."""
        day = self.now().strftime("%Y-%m-%d")
        try:
            rows = [r for r in trades_on(self.journal, day) if r["mode"] == self.broker.mode]
        except Exception as e:
            log.error("could not read today's trades to restore the day's limits: %s", e)
            return
        if not rows:
            return
        for r in rows:
            net = float(r["net_pnl"] or 0)
            self.om.realized_net += net
            self.om.charges_today += float(r["charges"] or 0)
            ts = datetime.fromisoformat(r["exit_ts"])
            self.risk.on_trade_closed(net, ts if ts.tzinfo else ts.replace(tzinfo=self.now().tzinfo))
        self.risk.trades_today = len(rows)
        if self.risk.cooldown_until and self.risk.cooldown_until <= self.now():
            self.risk.cooldown_until = None
        self.restored = {"trades": len(rows), "net": round(self.om.realized_net, 2),
                         "cooldown_until": self.risk.cooldown_until}
        log.warning("restored today's state from the journal: %d trades, net %.2f", len(rows), self.om.realized_net)
        self.journal.event(self.now(), "restored", f"trades={len(rows)} net={self.om.realized_net:.2f}")

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        if self.replay and self._now is not None:
            return self._now
        return now_ist()

    # ------------------------------------------------------------------ callbacks
    def _on_trade_closed(self, net: float, now: datetime) -> None:
        before = self.risk.cooldown_until
        self.risk.on_trade_closed(net, now)
        if self.risk.cooldown_until and self.risk.cooldown_until != before:
            notify.send(messages.cooldown(self.risk.cooldown_until, int(self.cfg["risk"]["max_consecutive_losses"])))

    def _on_bar(self, token: str, bar: Bar) -> None:
        inst = self.by_token.get(token)
        if not inst:
            return
        bars = self.bars_today[inst.symbol]
        if bars and bar.start.date() != bars[-1].start.date():
            bars.clear()
        bars.append(bar)
        pos = self.om.positions[inst.symbol]
        try:
            desired = self.strategies[inst.symbol].on_bar(bars, pos.side)
        except Exception as e:
            log.exception("strategy error on %s: %s", inst.symbol, e)
            self.journal.event(self.now(), "strategy_error", str(e), "ERROR")
            return
        if desired is None:
            return
        if desired < 0 and not self.allow_short:
            desired = 0
        self._act_on_signal(inst, desired, bar.close)

    def _act_on_signal(self, inst: Instrument, desired: int, price: float) -> None:
        now, pos = self.now(), self.om.positions[inst.symbol]
        side = pos.side
        acted, reason = False, ""
        if desired == side:
            reason = "already in desired state"
        elif self.om.has_pending(inst.symbol):
            reason = "orders pending"
        elif side != 0:
            # flatten first; if a reversal was asked, enter the other way once flat
            self.om.exit(inst.symbol, self.ltp.get(inst.symbol, price), now, "signal")
            if desired != 0:
                self.pending_reverse[inst.symbol] = desired
            acted, reason = True, "exit on signal"
        else:
            acted, reason = self._try_enter(inst, desired, price)
        self.journal.signal(now, inst.symbol, desired, side, price, acted, reason)
        self.recent_signals.append({"ts": now.isoformat(), "symbol": inst.symbol, "desired": desired,
                                    "acted": acted, "reason": reason})

    def _try_enter(self, inst: Instrument, direction: int, price: float) -> tuple[bool, str]:
        now = self.now()
        if self.control["paused"] or self.control["kill"] or self.flatten_reason:
            return False, "paused/killed/flattening"
        ltp = self.ltp.get(inst.symbol, price)
        qty, amt = inst.order_qty, sizing.amount(self.cfg)
        if amt:                                      # shares from the amount per trade, at the entry's limit price
            limit = max(ltp, self.om.entry_price(inst.symbol, direction, ltp))
            qty = sizing.qty_for(amt, limit, inst.lot_size)
            if not qty:
                return False, f"₹{amt:,.0f} per trade buys no share at ₹{limit:,.2f}"
        ok, why = self.risk.can_enter(now, self.om.open_positions(), qty * ltp)
        if not ok:
            return False, why
        oid = self.om.enter(inst.symbol, direction, ltp, now, f"{self.strategy_name} signal", qty=qty)
        if oid:
            self.risk.trades_today += 1
            return True, "entry placed"
        return False, "order placement failed"

    # ------------------------------------------------------------------ ticks
    def _on_tick(self, tick: Tick, cum_vol) -> None:
        inst = self.by_token.get(tick.token)
        if not inst:
            return
        if self.replay:
            self._now = tick.ts
        self.ltp[inst.symbol] = tick.ltp
        if hasattr(self.broker, "on_tick"):        # paper fills
            self.broker.on_tick(tick.token, tick.ltp)
        self.builder.on_tick(tick, cum_vol)
        self._manage_exits(inst, tick.ltp)

    def _manage_exits(self, inst: Instrument, ltp: float) -> None:
        pos = self.om.positions[inst.symbol]
        if pos.qty == 0 or self.om.has_pending(inst.symbol, "EXIT"):
            return
        trail = float(self.cfg["exits"].get("trailing_sl_pct") or 0) / 100
        if trail and pos.best_price is not None:
            if pos.qty > 0 and ltp > pos.best_price:
                pos.best_price = ltp
                pos.stop = max(pos.stop or 0, ltp * (1 - trail))
            elif pos.qty < 0 and ltp < pos.best_price:
                pos.best_price = ltp
                pos.stop = min(pos.stop or 1e18, ltp * (1 + trail))
        reason = None
        if pos.qty > 0:
            if pos.stop and ltp <= pos.stop:
                reason = "stop_loss"
            elif pos.target and ltp >= pos.target:
                reason = "target"
        else:
            if pos.stop and ltp >= pos.stop:
                reason = "stop_loss"
            elif pos.target and ltp <= pos.target:
                reason = "target"
        if reason:
            self.om.exit(inst.symbol, ltp, self.now(), reason)

    # ------------------------------------------------------------------ periodic
    def _periodic(self) -> None:
        now = self.now()
        self.builder.flush(now)
        self.om.poll(now, self.ltp, force=self.replay)

        # pending reversals once flat
        for sym, d in list(self.pending_reverse.items()):
            if self.om.positions[sym].qty == 0 and not self.om.has_pending(sym):
                self.pending_reverse.pop(sym)
                inst = next(i for i in self.instruments if i.symbol == sym)
                self._try_enter(inst, d, self.ltp.get(sym, 0))

        # control file (1s cadence in real time)
        if self.replay or time.monotonic() - self._last_control > 1.0:
            self._last_control = time.monotonic()
            c = read_control()
            if c != self.control:
                log.warning("control changed: %s", c)
                self.journal.event(now, "control", str(c))
            self.control = c

        # daily P&L limits
        day_pnl = self.om.realized_net + self.om.unrealized(self.ltp)
        halt = self.risk.check_pnl_limits(day_pnl)
        if halt and not self.flatten_reason:
            self._flatten(halt)
        if self.control["kill"] and not self.flatten_reason:
            self._flatten("KILL SWITCH")
        if self.risk.must_squareoff(now) and not self.flatten_reason:
            self._flatten("squareoff time")
        if self.flatten_reason:
            self._flatten_tick()

        # morning heartbeat: one "I'm alive and ready" message when entries open (live only)
        if not self.replay and not self._heartbeat_sent and now >= at(now, self.cfg["session"]["entry_start"]):
            self._heartbeat_sent = True
            notify.send(messages.heartbeat(bool(getattr(self.feed, "connected", False)), dict(self.ltp),
                                           bool(self.control["paused"]), self.cfg["risk"]["max_daily_loss"]))

        # feed watchdog (live only, market hours)
        if not self.replay and at(now, "09:15") <= now <= at(now, "15:30"):
            stale = float(self.cfg.get("feed", {}).get("stale_after_sec", 30))
            last = self.feed.last_tick_at
            ref = max(last, at(now, "09:15")) if last else at(now, "09:15")      # no ticks before the open are expected
            dead = (not self.feed.connected) or (now - ref).total_seconds() > stale
            base = max(stale, 30)
            if dead and time.monotonic() - self._last_feed_restart > max(base, self._feed_wait):
                # back off while restarts keep failing (30 s, 60 s, 2 min, 4 min, then every 5 min), so a broker
                # that is refusing connections is not hammered; reset once ticks flow again
                self._feed_wait = min(300.0, self._feed_wait * 2) if self._feed_wait else base
                self._last_feed_restart = time.monotonic()
                self.journal.event(now, "feed_restart", "stale/disconnected feed", "WARNING")
                self._feed_alerted = True
                notify.send(messages.feed_trouble(last.strftime("%H:%M:%S") if last else None,
                                                  getattr(self.feed, "restarts", 0) + 1))
                self.feed.restart()
            elif not dead and self._feed_alerted:
                self._feed_alerted = False
                self._feed_wait = 0.0
                notify.send(messages.feed_back())

        if self.replay or time.monotonic() - self._last_status > 2.0:
            self._last_status = time.monotonic()
            self._write_status(day_pnl)

    def _flatten(self, reason: str) -> None:
        self.flatten_reason = reason
        self.state = "flattening"
        log.warning("FLATTEN ALL: %s", reason)
        self.journal.event(self.now(), "flatten", reason, "WARNING")
        notify.send(messages.flatten(reason))
        for oid in list(self.om.pending):
            rec = self.om.pending[oid]
            if rec.purpose == "ENTRY":
                self.om._safe_cancel(rec.order_id, self.now(), reason)
        self._flatten_tick()

    def _flatten_tick(self) -> None:
        for sym, pos in self.om.positions.items():
            if pos.qty and not self.om.has_pending(sym, "EXIT") and sym in self.ltp:
                self.om.exit(sym, self.ltp[sym], self.now(), self.flatten_reason or "flatten")

    def _write_status(self, day_pnl: float) -> None:
        positions = []
        for sym, p in self.om.positions.items():
            if p.qty:
                positions.append({"symbol": sym, "qty": p.qty, "avg": round(p.avg_price, 2),
                                  "ltp": self.ltp.get(sym), "unrealized": round(p.unrealized(self.ltp.get(sym, p.avg_price)), 2),
                                  "stop": round(p.stop, 2) if p.stop else None,
                                  "target": round(p.target, 2) if p.target else None})
        write_status({
            "state": self.state, "mode": self.broker.mode, "replay": self.replay,
            "strategy": self.strategies[self.instruments[0].symbol].describe(),
            "instruments": [i.symbol for i in self.instruments],
            "now": self.now().isoformat(),
            "day_pnl": round(day_pnl, 2), "realized_net": round(self.om.realized_net, 2),
            "charges_today": round(self.om.charges_today, 2),
            "trades_today": self.risk.trades_today,
            "halted_reason": self.risk.halted_reason, "flatten_reason": self.flatten_reason,
            "cooldown_until": self.risk.cooldown_until.isoformat() if self.risk.cooldown_until else None,
            "control": self.control, "positions": positions,
            "pending_orders": [{"id": o.order_id, "symbol": o.symbol, "side": o.side, "qty": o.qty,
                                "price": o.price, "purpose": o.purpose, "chases": o.chases}
                               for o in self.om.pending.values()],
            "ltp": self.ltp,
            "feed": {"connected": getattr(self.feed, "connected", None),
                     "last_tick_at": self.feed.last_tick_at.isoformat() if getattr(self.feed, "last_tick_at", None) else None,
                     "restarts": getattr(self.feed, "restarts", 0)},
            "recent_signals": list(self.recent_signals),
            "strategy_state": self._strategy_state(),
        })

    def _strategy_state(self) -> dict:
        """What each instrument's strategy sees right now (display only; a failure here never stops the bot)."""
        out = {}
        for sym, strat in self.strategies.items():
            try:
                out[sym] = strat.state(self.bars_today.get(sym) or [], self.ltp.get(sym))
            except Exception as e:
                out[sym] = {"error": str(e)}
        return out

    def _send_daily_summary(self) -> None:
        """End-of-run Telegram card built from today's closed trades (message only)."""
        day = self.now().strftime("%Y-%m-%d")
        try:
            rows = trades_on(self.journal, day)
        except Exception as e:
            log.warning("summary query failed: %s", e)
            rows = []
        notify.send("⏹ <b>AngelBot stopped</b>\n\n" +
                    messages.daily_summary(rows, day, self.broker.mode, title="Day summary"))

    # ------------------------------------------------------------------ lifecycle
    def reconcile(self) -> None:
        """Adopt open positions at the broker for our symbols (crash-restart safety)."""
        try:
            for p in self.broker.positions():
                if p["symbol"] in self.om.positions and p["netqty"] != 0:
                    self.om.adopt(p["symbol"], p["netqty"], p["avg"], self.now())
                    self.risk.trades_today += 1          # its entry counted towards today's trades before the restart
                    notify.send(messages.adopted(p["symbol"], p["netqty"], p["avg"]))
        except Exception as e:
            log.error("reconcile failed: %s", e)

    def prefill(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        """Seed today's bars (e.g. after a mid-day restart) so indicators are warm."""
        for sym, bars in bars_by_symbol.items():
            self.bars_today[sym] = list(bars)
            log.info("prefilled %d bars for %s", len(bars), sym)

    def run(self) -> int:
        self.state = "running"
        self.journal.event(self.now(), "start", f"mode={self.broker.mode} replay={self.replay} profile={os.environ.get('ANGELBOT_PROFILE', '')}")
        from bot import settings
        notify.send(messages.started(self.broker.mode, self.strategy_name, [i.symbol for i in self.instruments],
                                     None if self.replay else settings.summary() + [f"⚠️ {x}" for x in settings.problems()],
                                     self.restored))
        self.feed.start()
        ended = False
        try:
            while True:
                try:
                    item = self.q.get(timeout=0.5)
                    if item is None:
                        ended = True
                    else:
                        self._on_tick(*item)
                except queue.Empty:
                    pass
                self._periodic()
                now = self.now()
                flat = self.om.open_positions() == 0 and not self.om.pending
                if self.control["kill"] and flat:
                    log.warning("kill switch: flat, exiting")
                    self.state = "stopped"
                    break
                if self.risk.should_shutdown(now) and (flat or now >= at(now, self.cfg["session"]["shutdown"]) + timedelta(minutes=10)):
                    if not flat:
                        msg = "shutdown with OPEN POSITIONS/ORDERS — check broker terminal!"
                        log.error(msg); notify.send(messages.shutdown_open())
                        self.exit_code = 2
                    self.state = "stopped"
                    break
                if ended:
                    # replay finished: square off whatever is left at last price
                    if self.om.open_positions() and not self.flatten_reason:
                        self._flatten("end of replay data")
                    for _ in range(20):
                        self._periodic()
                        if self.om.open_positions() == 0 and not self.om.pending:
                            break
                    self.state = "stopped"
                    break
        except KeyboardInterrupt:
            log.warning("interrupted — leaving positions to stops/broker; run again or flatten manually")
            self.state = "stopped"
            self.exit_code = 130
        finally:
            self.feed.stop()
            self._write_status(self.om.realized_net + self.om.unrealized(self.ltp))
            self.journal.event(self.now(), "stop", f"state={self.state} pnl={self.om.realized_net:.2f}")
            self._send_daily_summary()
            if self.control.get("kill"):
                set_control(kill=False, paused=True, note="auto-reset after kill; bot left paused")
        return self.exit_code
