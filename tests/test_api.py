import pytest
from starlette.testclient import TestClient

from bot import alerts, api_server as API, control
from bot import settings as ST
from bot.journal import Journal

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    for p in (control.STATUS, alerts.path(), ST.path()):
        p.unlink(missing_ok=True)
    with TestClient(API.create_app(TOKEN, tls=True)) as c:
        yield c


def test_startup_refuses_wrong_area_and_public_addresses(monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "live")
    with pytest.raises(SystemExit, match="only for the paper area"):
        API.check_area()
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    assert API.check_area() == "paper"
    API.check_bind("100.101.102.103", dev=False)                 # Tailscale IPv4
    API.check_bind("fd7a:115c:a1e0::1", dev=False)               # Tailscale IPv6
    API.check_bind("127.0.0.1", dev=True)
    for bad in ("0.0.0.0", "10.0.0.5", "152.67.1.2", "127.0.0.1", "::", "example.com"):
        with pytest.raises(SystemExit):
            API.check_bind(bad, dev=False)
    with pytest.raises(SystemExit, match="HTTPS is required"):
        API.main(["--host", "100.101.102.103"])


def test_every_api_route_needs_the_token(client):
    for path in ("/api/status", "/api/trades", "/api/settings", "/api/alerts", "/api/report", "/api/screener"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401, path
    assert client.get("/healthz").json() == {"ok": True}


def test_read_only_and_no_order_or_live_routes(client):
    """Exactly these routes exist, all read-only. Anything that could trade, switch mode or touch secrets is absent."""
    paths = {r.path for r in API.create_app(TOKEN).routes}
    assert paths == {"/healthz", "/ws", "/api/status", "/api/trades", "/api/settings", "/api/alerts", "/api/report",
                     "/api/screener"}
    for path in paths - {"/ws"}:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)(path, headers=AUTH).status_code == 405, (method, path)


def test_security_headers(client):
    r = client.get("/api/status", headers=AUTH)
    for k in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Strict-Transport-Security"):
        assert k in r.headers, k
    assert r.headers["Cache-Control"] == "no-store" and "access-control-allow-origin" not in r.headers
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert client.get("/api/status").headers["X-Frame-Options"] == "DENY"          # also on refusals


def test_status_settings_alerts(client):
    control.write_status({"state": "running", "mode": "paper", "positions": [], "strategy_state": {"TCS-EQ": {"bars": 30}}})
    s = client.get("/api/status", headers=AUTH).json()
    assert s["state"] == "running" and s["bot_alive"] and "control" in s and s["strategy_state"]["TCS-EQ"]["bars"] == 30
    st = client.get("/api/settings", headers=AUTH).json()
    amount = next(x for x in st["settings"] if x["id"] == "amount")
    assert st["area"] == "paper" and amount["typed"] and any(c["current"] for c in amount["choices"])
    alerts.append("<b>a</b>", "a", "PAPER")
    got = client.get("/api/alerts?limit=5", headers=AUTH).json()["alerts"]
    assert got[-1]["text"] == "a"
    assert client.get("/api/alerts?limit=x", headers=AUTH).status_code == 400


def test_trades_by_day_with_totals_and_date_check(client):
    from datetime import datetime
    from bot.timeutil import IST
    control.JOURNAL.unlink(missing_ok=True)
    j = Journal(control.JOURNAL)
    t = lambda h: datetime.fromisoformat(f"2026-10-22 {h}").replace(tzinfo=IST)
    j.trade("paper", "SBIN-EQ", "LONG", 5, t("10:00"), 966, t("10:20"), 970, 20.0, 13.3, "target", "ema_cross")
    j.trade("paper", "SBIN-EQ", "LONG", 5, t("11:00"), 970, t("11:10"), 968, -10.0, 13.3, "stop_loss", "ema_cross")
    r = client.get("/api/trades?date=2026-10-22", headers=AUTH).json()
    assert r["totals"] == {"trades": 2, "net_pnl": -16.6, "charges": 26.6, "gross_pnl": 10.0, "win_rate_pct": 50.0}
    assert client.get("/api/trades?date=2026-10-22' OR 1=1 --", headers=AUTH).status_code == 400
    assert client.get("/api/trades?date=2026-10-21", headers=AUTH).json()["trades"] == []


def test_report_and_screener_say_when_there_is_nothing_yet(client):
    control.SCREEN.unlink(missing_ok=True)
    r = client.get("/api/screener", headers=AUTH)
    assert r.status_code == 404 and "No screener snapshot" in r.json()["error"]


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(API, "READS_PER_MIN", 3)
    with TestClient(API.create_app(TOKEN)) as c:
        codes = [c.get("/api/status", headers=AUTH).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_websocket_needs_the_token_first_then_streams_status(client, monkeypatch):
    monkeypatch.setattr(API, "WS_PUSH_SEC", 0.05)
    control.write_status({"state": "running", "mode": "paper"})
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"auth": TOKEN})
        msg = ws.receive_json()
        assert msg["type"] == "status" and msg["status"]["state"] == "running"
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"auth": "wrong"})
        with pytest.raises(Exception):
            ws.receive_json()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_token_file_is_private(monkeypatch):
    API.token_path().unlink(missing_ok=True)
    tok = API.load_or_create_token()
    assert len(tok) >= 40 and oct(API.token_path().stat().st_mode & 0o777) == "0o600"
    assert API.load_or_create_token() == tok                     # kept across restarts
