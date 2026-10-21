"""Market data feeds → a queue of (Tick, cumulative_volume) items.

LiveFeed   : SmartAPI WebSocket V2 in a background thread, with restart().
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

    def _token_list(self):
        groups: dict[int, list[str]] = {}
        for i in self.instruments:
            groups.setdefault(i.ws_exchange_type, []).append(i.token)
        return [{"exchangeType": k, "tokens": v} for k, v in groups.items()]

    def start(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        s = self.broker.session
        self.ws = SmartWebSocketV2(s["jwtToken"], self.broker.creds["api_key"],
                                   self.broker.creds["client_code"], self.broker.feed_token,
                                   max_retry_attempt=5, retry_strategy=1, retry_delay=5)

        def on_open(wsapp):
            self.connected = True
            log.info("websocket open; subscribing %d tokens", len(self.instruments))
            self.ws.subscribe("angelbot", self.QUOTE_MODE, self._token_list())

        def on_data(wsapp, msg):
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
            self.connected = False
            log.warning("websocket error: %s", args)

        def on_close(wsapp):
            self.connected = False
            log.warning("websocket closed")

        self.ws.on_open, self.ws.on_data = on_open, on_data
        self.ws.on_error, self.ws.on_close = on_error, on_close
        self.thread = threading.Thread(target=self._run, name="feed", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.ws.connect()
        except Exception as e:
            log.error("feed thread exited: %s", e)
        finally:
            self.connected = False

    def restart(self) -> None:
        self.restarts += 1
        log.warning("restarting feed (#%d)", self.restarts)
        self.stop()
        self.start()

    def stop(self) -> None:
        try:
            if self.ws:
                self.ws.close_connection()
        except Exception:
            pass


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
