"""Market data feeds → a queue of (Tick, cumulative_volume) items.

LiveFeed   : SmartAPI WebSocket V2 in a background thread, with restart().
             Only ONE connection is ever live: the library's own reconnect loop is switched off (it reconnects from
             inside its error callback, so an old connection could come back after our restart and the broker then
             refuses new ones with 429 "Connection Limit Exceeded"); the engine's watchdog alone reconnects, and
             callbacks from a replaced connection are ignored.
ReplayFeed : replays historical candles as synthetic ticks (offline dry runs/tests).
"""
from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime, timedelta

from bot.models import Instrument, Tick
from bot.timeutil import from_epoch_ms

log = logging.getLogger("angelbot.feed")


class LiveFeed:
    QUOTE_MODE = 2  # LTP + volume + OHLC

    def __init__(self, broker, instruments: list[Instrument], q: queue.Queue):
        self.broker = broker
        self.instruments = instruments
        self.q = q
        self.ws = None
        self.thread: threading.Thread | None = None
        self.last_tick_at: datetime | None = None
        self.connected = False
        self.restarts = 0
        self._gen = 0              # connection generation: callbacks from an older one are ignored

    def _token_list(self):
        groups: dict[int, list[str]] = {}
        for i in self.instruments:
            groups.setdefault(i.ws_exchange_type, []).append(i.token)
        return [{"exchangeType": k, "tokens": v} for k, v in groups.items()]

    def start(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        s = self.broker.session
        self._gen += 1
        gen = self._gen
        # max_retry_attempt=0: no reconnects inside the library; the engine's watchdog restarts the feed instead
        ws = SmartWebSocketV2(s["jwtToken"], self.broker.creds["api_key"],
                              self.broker.creds["client_code"], self.broker.feed_token, max_retry_attempt=0)
        current = lambda: gen == self._gen

        def on_open(wsapp):
            if not current():                      # a replaced connection that opened late: close it at once
                log.warning("closing a replaced websocket that opened late")
                ws.close_connection()
                return
            self.connected = True
            log.info("websocket open; subscribing %d tokens", len(self.instruments))
            ws.subscribe("angelbot", self.QUOTE_MODE, self._token_list())

        def on_data(wsapp, msg):
            if not current():
                return
            try:
                ltp = msg.get("last_traded_price")
                if ltp is None:
                    return
                ts = from_epoch_ms(msg["exchange_timestamp"]) if msg.get("exchange_timestamp") else None
                tick = Tick(token=str(msg["token"]), ltp=ltp / 100.0, ts=ts or datetime.now().astimezone())
                self.last_tick_at = tick.ts
                self.q.put((tick, msg.get("volume_trade_for_the_day")))
            except Exception as e:
                log.exception("bad tick %s: %s", msg, e)

        def on_error(*args):
            if current():
                self.connected = False
                log.warning("websocket error: %s", args)

        def on_close(wsapp, *args):
            if current():
                self.connected = False
                log.warning("websocket closed %s", args or "")

        ws.on_open, ws.on_data, ws.on_error, ws.on_close = on_open, on_data, on_error, on_close
        # websocket-client calls on_close(ws, status_code, reason); the library's _on_close takes only (ws), which
        # raised "takes 2 positional arguments but 4 were given" on every close and skipped our handler
        ws._on_close = lambda wsapp, *args: on_close(wsapp, *args)
        self.ws = ws
        self.thread = threading.Thread(target=self._run, args=(ws, gen), name=f"feed-{gen}", daemon=True)
        self.thread.start()

    def _run(self, ws, gen):
        try:
            ws.connect()
        except Exception as e:
            log.error("feed thread exited: %s", e)
        finally:
            if gen == self._gen:
                self.connected = False

    def restart(self) -> None:
        self.restarts += 1
        log.warning("restarting feed (#%d)", self.restarts)
        self.stop()
        self.start()

    def stop(self, join_sec: float = 5.0) -> None:
        """Close the current connection and wait briefly for its thread, so it is gone before a new one opens."""
        ws, thread = self.ws, self.thread
        self._gen += 1                             # from now on its callbacks are ignored, even if it lingers
        self.connected = False
        try:
            if ws:
                ws.close_connection()
        except Exception:
            pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(join_sec)
            if thread.is_alive():
                log.warning("old websocket thread still closing; its data is ignored")


class ReplayFeed:
    """Turns 1-min candles into 4 synthetic ticks each (O, H/L, L/H, C)."""

    def __init__(self, candles_by_token: dict[str, list[list]], q: queue.Queue):
        self.q = q
        self.items: list[tuple[Tick, float]] = []
        for token, candles in candles_by_token.items():
            cum = 0.0
            for c in candles:
                ts = c[0] if isinstance(c[0], datetime) else datetime.fromisoformat(c[0])
                o, h, l, cl, v = map(float, c[1:6])
                path = [o, l, h, cl] if cl >= o else [o, h, l, cl]
                for k, px in enumerate(path):
                    cum += v / 4
                    self.items.append((Tick(token, px, ts + timedelta(seconds=5 + k * 15)), cum))
        self.items.sort(key=lambda it: it[0].ts)
        self.connected = True
        self.last_tick_at = None
        self.restarts = 0

    def start(self) -> None:
        for it in self.items:
            self.q.put(it)
        self.q.put(None)  # end-of-data sentinel

    def restart(self) -> None:
        pass

    def stop(self) -> None:
        pass
