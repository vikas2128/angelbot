"""LiveBroker / LiveFeed against fake SmartAPI objects (no network)."""
import queue
import sys
import types

from bot.broker import LiveBroker, BrokerError
from bot.feed import LiveFeed
from bot.risk import OrderThrottle

import pytest


class FakeApi:
    def __init__(self):
        self.placed, self.fail_auth_once = [], False
    def generateSession(self, c, p, totp):
        return {"status": True, "data": {"clientcode": c, "jwtToken": "Bearer X", "feedToken": "F"}}
    def placeOrderFullResponse(self, params):
        self.placed.append(params)
        return {"status": True, "data": {"orderid": "250101000001"}}
    def orderBook(self):
        if self.fail_auth_once:
            self.fail_auth_once = False
            return {"status": False, "errorcode": "AG8001", "message": "Invalid Token"}
        return {"status": True, "data": [
            {"orderid": "250101000001", "status": "complete", "filledshares": "1", "averageprice": 801.5, "text": ""},
            {"orderid": "250101000002", "status": "rejected", "filledshares": "0", "averageprice": 0, "text": "RMS: margin"},
            {"orderid": "250101000003", "status": "open pending", "filledshares": "0", "averageprice": 0, "text": ""}]}
    def position(self):
        return {"status": True, "data": [
            {"tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "netqty": "-2", "netprice": "805.1", "producttype": "INTRADAY"},
            {"tradingsymbol": "INFY-EQ", "symboltoken": "1594", "netqty": "0", "netprice": "0"}]}
    def getfeedToken(self):
        return "F"


@pytest.fixture
def broker(monkeypatch):
    fake_mod = types.SimpleNamespace(SmartConnect=lambda api_key: FakeApi())
    monkeypatch.setitem(sys.modules, "SmartApi", fake_mod)
    b = LiveBroker({"api_key": "k", "client_code": "C1", "pin": "1", "totp_secret": "JBSWY3DPEHPK3PXP"},
                   OrderThrottle(5))
    b.login()
    return b


def test_place_is_limit_only_with_tag(broker, inst):
    oid = broker.place_limit(inst, "BUY", 1, 801.45)
    p = broker.api.placed[-1]
    assert oid == "250101000001"
    assert p["ordertype"] == "LIMIT" and p["price"] == "801.45" and p["ordertag"] == "angelbot"
    assert p["duration"] == "DAY" and p["variety"] == "NORMAL"


def test_order_status_mapping_and_relogin(broker):
    broker.api.fail_auth_once = True
    st = broker.order_statuses(["250101000001", "250101000002", "250101000003"])
    assert st["250101000001"]["status"] == "complete" and st["250101000001"]["filled_qty"] == 1
    assert st["250101000002"]["status"] == "rejected" and "margin" in st["250101000002"]["reason"]
    assert st["250101000003"]["status"] == "open"


def test_positions_parsing(broker):
    pos = broker.positions()
    assert pos == [{"symbol": "SBIN-EQ", "token": "3045", "netqty": -2, "avg": 805.1, "product": "INTRADAY"}]


def test_live_feed_parses_ticks(broker, inst, monkeypatch):
    captured = {}

    class FakeWS:
        def __init__(self, *a, **k):
            captured["ws"] = self
        def subscribe(self, cid, mode, tokens):
            captured["sub"] = (mode, tokens)
        def connect(self):
            self.on_open(None)
            self.on_data(None, {"token": "3045", "last_traded_price": 80150,
                                "exchange_timestamp": 1792000000000, "volume_trade_for_the_day": 12345})
        def close_connection(self):
            pass

    monkeypatch.setitem(sys.modules, "SmartApi.smartWebSocketV2", types.SimpleNamespace(SmartWebSocketV2=FakeWS))
    q = queue.Queue()
    f = LiveFeed(broker, [inst], q)
    f.start(); f.thread.join(2)
    tick, vol = q.get(timeout=2)
    assert tick.ltp == 801.5 and tick.token == "3045" and vol == 12345
    assert captured["sub"] == (2, [{"exchangeType": 1, "tokens": ["3045"]}])


def test_rate_limit_backoff_then_success(broker, monkeypatch):
    import bot.broker as bb
    monkeypatch.setattr(bb.time, "sleep", lambda s: None)
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'")
        return {"status": True, "data": [1]}
    assert broker._call(flaky) == {"status": True, "data": [1]} and calls["n"] == 3


def test_rate_limit_gives_up(broker, monkeypatch):
    import bot.broker as bb
    monkeypatch.setattr(bb.time, "sleep", lambda s: None)
    def always():
        raise Exception("Access denied because of exceeding access rate")
    with pytest.raises(BrokerError):
        broker._call(always)
