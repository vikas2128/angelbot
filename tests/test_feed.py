"""LiveFeed with a fake SmartWebSocketV2: one live connection at a time, close callbacks that work, no library retries."""
import queue
import sys
import threading
import types
from datetime import datetime, timedelta

import pytest

from bot.feed import LiveFeed
from bot.models import Instrument
from bot.timeutil import IST


class FakeWS:
    made: list["FakeWS"] = []

    def __init__(self, *args, max_retry_attempt=1, **kw):
        self.max_retry_attempt = max_retry_attempt
        self.closed = threading.Event()
        self.subscribed = []
        FakeWS.made.append(self)

    def _on_close(self, wsapp):                      # the library's broken signature
        self.on_close(wsapp)

    def connect(self):                               # like run_forever: blocks until closed, then reports the close
        self.on_open(None)
        self.closed.wait(5)
        self._on_close(None, 1000, "bye")            # websocket-client passes (ws, code, reason)

    def close_connection(self):
        self.closed.set()

    def subscribe(self, cid, mode, tokens):
        self.subscribed.append(tokens)


@pytest.fixture
def feed(monkeypatch):
    FakeWS.made = []
    mod = types.ModuleType("SmartApi.smartWebSocketV2")
    mod.SmartWebSocketV2 = FakeWS
    monkeypatch.setitem(sys.modules, "SmartApi.smartWebSocketV2", mod)
    broker = types.SimpleNamespace(session={"jwtToken": "j"}, creds={"api_key": "k", "client_code": "c"}, feed_token="f")
    f = LiveFeed(broker, [Instrument("SBIN-EQ", "3045", "NSE", 1, 0.05, 1, "INTRADAY")], queue.Queue())
    yield f
    f.stop()


def _wait(cond, sec=2.0):
    end = datetime.now() + timedelta(seconds=sec)
    while not cond() and datetime.now() < end:
        pass
    return cond()


def test_library_retries_are_off_and_close_no_longer_raises(feed):
    feed.start()
    assert _wait(lambda: FakeWS.made and FakeWS.made[0].subscribed) and feed.connected
    ws = FakeWS.made[0]
    assert ws.max_retry_attempt == 0
    ws.close_connection()                            # the server drops us: our on_close must run, not raise
    assert _wait(lambda: not feed.thread.is_alive()) and feed.connected is False


def test_restart_closes_the_old_connection_first_and_ignores_its_data(feed):
    feed.start()
    assert _wait(lambda: feed.connected)
    old = FakeWS.made[0]
    old_thread = feed.thread
    feed.restart()
    assert old.closed.is_set() and not old_thread.is_alive()           # gone before the new one opened
    assert len(FakeWS.made) == 2 and _wait(lambda: feed.connected)
    old.on_data(None, {"token": "3045", "last_traded_price": 99900, "exchange_timestamp": None})
    assert feed.q.empty()                                                # a replaced connection's ticks are dropped
    FakeWS.made[1].on_data(None, {"token": "3045", "last_traded_price": 80000, "exchange_timestamp": None})
    assert feed.q.get_nowait()[0].ltp == 800.0
    old.on_error("late error")                                           # cannot mark the new connection as down
    assert feed.connected is True


def test_a_replaced_connection_that_opens_late_closes_itself(feed):
    feed.start()
    assert _wait(lambda: feed.connected)
    old = FakeWS.made[0]
    feed.stop()
    old.closed.clear()
    old.on_open(None)                                                    # e.g. a reconnect finishing after stop()
    assert old.closed.is_set() and feed.connected is False
