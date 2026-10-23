"""Charts: the bot shares today's bars; the API serves one traded stock's bars to a signed-in device."""
import queue
from datetime import datetime

import pytest

from bot import control
from bot import api_server as API
from bot.engine import Engine
from bot.journal import Journal
from bot.paper import PaperBroker
from bot.timeutil import IST, now_ist
from conftest import make_bars
from test_api import _client, _signed_in


def test_engine_writes_todays_bars_on_each_close_and_never_in_replays(cfg, inst, tmp_path, monkeypatch):
    from bot import notify
    from test_trading import _FakeFeed
    monkeypatch.setattr(notify, "send", lambda t, b=None: None)
    control.BARS.unlink(missing_ok=True)
    eng = Engine(cfg, PaperBroker(0.0), _FakeFeed(), [inst], Journal(tmp_path / "j.db"), queue.Queue())
    today = now_ist().strftime("%Y-%m-%d")
    eng.now = lambda: datetime.fromisoformat(f"{today} 10:00").replace(tzinfo=IST)
    eng.prefill({"SBIN-EQ": make_bars([800 + i * 0.1 for i in range(5)], start=f"{today} 09:15")})
    d = control.read_json(control.BARS)
    assert d["date"] == today and len(d["bars"]["SBIN-EQ"]) == 5 and d["bars"]["SBIN-EQ"][0][0].startswith(f"{today}T09:15")
    eng._on_bar(inst.token, make_bars([801.0], start=f"{today} 09:20")[0])
    assert len(control.read_json(control.BARS)["bars"]["SBIN-EQ"]) == 6
    control.BARS.unlink()
    rep = Engine(cfg, PaperBroker(0.0), _FakeFeed(), [inst], Journal(tmp_path / "r.db"), queue.Queue(), replay=True)
    rep.prefill({"SBIN-EQ": make_bars([800.0] * 3, start=f"{today} 09:15")})
    assert not control.BARS.exists()


def test_bars_route_checks_session_symbol_and_day(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    today = now_ist().strftime("%Y-%m-%d")
    control.write_json(control.BARS, {"date": today, "timeframe_min": 1,
                                      "bars": {"TCS-EQ": [[f"{today}T09:15:00+05:30", 1, 2, 0.5, 1.5, 10]] * 3}})
    store, c = _client(tmp_path)
    with c:
        assert c.get("/api/bars?symbol=TCS-EQ").status_code == 401
        _signed_in(store, c)
        r = c.get("/api/bars?symbol=TCS-EQ").json()
        assert r["symbol"] == "TCS-EQ" and len(r["bars"]) == 3 and r["timeframe_min"] == 1
        assert c.get("/api/bars?symbol=INFY-EQ").status_code == 404                       # not traded today
        for bad in ("tcs-eq", "../etc", "TCS EQ", "", "X" * 40):
            assert c.get("/api/bars", params={"symbol": bad}).status_code == 400, bad
        control.write_json(control.BARS, {"date": "2020-01-01", "bars": {"TCS-EQ": [[1, 2, 3, 4, 5, 6]]}})
        assert c.get("/api/bars?symbol=TCS-EQ").status_code == 404                        # yesterday's file
    control.BARS.unlink()
