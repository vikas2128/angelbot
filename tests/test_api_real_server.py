"""The API under a REAL uvicorn server (not the test client), over the network: catches what the in-process test
client cannot, such as a missing WebSocket library or a header value the HTTP layer rejects."""
import asyncio
import socket
import threading
import time
from datetime import datetime

import pytest
import requests

from bot import api_auth as AA
from bot import api_server as API
from bot import control
from bot.journal import Journal
from bot.timeutil import IST, now_ist
from passkey import SoftKey


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    import uvicorn
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    monkeypatch.setattr(API, "WS_PUSH_SEC", 0.1)
    port = _free_port()
    origin = f"http://localhost:{port}"
    store = AA.AuthStore("localhost", origin, folder=tmp_path / "app", notify_fn=lambda t: None)
    srv = uvicorn.Server(uvicorn.Config(API.create_app(store, tls=False), host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    key = SoftKey("localhost", origin)
    o = store.register_options(store.create_enrol_code("Phone"), "127.0.0.1")
    store.register_verify(o["id"], key.create(o["options"]), "127.0.0.1")
    o = store.login_options("127.0.0.1")
    token, _ = store.login_verify(o["id"], key.get(o["options"]), "127.0.0.1")
    yield f"http://127.0.0.1:{port}", origin, {AA.COOKIE: token}
    srv.should_exit = True
    th.join(15)
    assert not th.is_alive(), "uvicorn did not stop"           # fully gone before the next test (no stray threads)


def test_pages_report_and_websocket_work_under_uvicorn(server):
    base, origin, cookies = server
    assert requests.get(base + "/", timeout=5).status_code == 200
    assert requests.get(base + "/api/status", timeout=5).status_code == 401
    control.JOURNAL.unlink(missing_ok=True)
    j = Journal(control.JOURNAL)
    d = now_ist().strftime("%Y-%m-%d")
    t = lambda hm: datetime.fromisoformat(f"{d} {hm}").replace(tzinfo=IST)
    j.trade("paper", "TCS-EQ", "LONG", 5, t("09:41"), 2577.7, t("10:02"), 2574.3, -17.0, 34.73, "signal", "ema_cross")
    r = requests.get(base + "/api/report", cookies=cookies, timeout=10)
    assert r.status_code == 200 and "AngelBot report" in r.text                   # every header is valid HTTP

    import websockets
    control.write_status({"state": "running", "mode": "paper"})

    async def live():
        url = base.replace("http", "ws") + "/ws"
        async with websockets.connect(url, origin=origin, additional_headers={"Cookie": f"{AA.COOKIE}={cookies[AA.COOKIE]}"}) as ws:
            return await asyncio.wait_for(ws.recv(), 5)
    msg = asyncio.run(live())
    assert '"type": "status"' in msg or '"type":"status"' in msg
