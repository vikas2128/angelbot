import pytest
from starlette.testclient import TestClient

from bot import alerts, api_server as API, control
from bot import api_auth as AA
from bot import settings as ST
from bot.journal import Journal
from passkey import SoftKey

RP = "bot.tail1234.ts.net"
ORIGIN = f"https://{RP}"
H = {"origin": ORIGIN, "x-requested-with": "angelbot"}
AUTH = {}                                            # reads go by the session cookie now


def _signed_in(store, c):
    key = SoftKey(RP, ORIGIN)
    r = c.post("/auth/register/options", json={"code": store.create_enrol_code("Phone")}, headers=H).json()
    c.post("/auth/register/verify", json={"id": r["id"], "credential": key.create(r["options"])}, headers=H)
    r = c.post("/auth/login/options", headers=H).json()
    c.post("/auth/login/verify", json={"id": r["id"], "credential": key.get(r["options"])}, headers=H)


def _client(tmp_path):
    store = AA.AuthStore(RP, ORIGIN, folder=tmp_path / "app", notify_fn=lambda t: None)
    return store, TestClient(API.create_app(store, tls=True), base_url=ORIGIN)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    for p in (control.STATUS, alerts.path(), ST.path()):
        p.unlink(missing_ok=True)
    store, c = _client(tmp_path)
    with c:
        _signed_in(store, c)
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


def test_every_api_route_needs_a_session(client):
    client.cookies.clear()
    for path in ("/api/status", "/api/trades", "/api/settings", "/api/alerts", "/api/report", "/api/screener", "/auth/devices"):
        assert client.get(path).status_code == 401, path
    assert client.get("/healthz").json() == {"ok": True}


def test_route_inventory_no_order_or_live_routes(client):
    """Exactly these routes exist. Data routes are GET-only; actions are the Telegram ones. Nothing can place orders,
    switch paper/live or touch secrets."""
    app = API.create_app(AA.AuthStore(RP, ORIGIN, folder=None, notify_fn=lambda t: None))
    paths = {r.path for r in app.routes}
    data = {"/api/status", "/api/trades", "/api/settings", "/api/alerts", "/api/report", "/api/screener"}
    actions = {"/api/control/pause", "/api/control/resume", "/api/control/kill/ask", "/api/control/kill/confirm",
               "/api/control/restart", "/api/settings/set", "/api/settings/reset"}
    auth = {"/auth/me", "/auth/register/options", "/auth/register/verify", "/auth/login/options", "/auth/login/verify",
            "/auth/stepup/options", "/auth/stepup/verify", "/auth/logout", "/auth/devices", "/auth/devices/remove"}
    assert paths == data | actions | auth | {"/healthz", "/ws"} | set(API.STATIC)
    for word in ("order", "buy", "sell", "live", "mode", "env", "profile", "broker", "shell", "exec", "sql", "file"):
        assert not any(word in p for p in paths), word
    for path in data:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)(path, headers=H).status_code == 405, (method, path)


def test_security_headers(client):
    r = client.get("/api/status", headers=AUTH)
    for k in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Strict-Transport-Security"):
        assert k in r.headers, k
    assert r.headers["Cache-Control"] == "no-store" and "access-control-allow-origin" not in r.headers
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    client.cookies.clear()
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


def test_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    monkeypatch.setattr(API, "READS_PER_MIN", 8)
    store, c = _client(tmp_path)
    with c:
        _signed_in(store, c)                                          # 4 login steps count too
        codes = [c.get("/api/status").status_code for _ in range(6)]
    assert codes == [200, 200, 200, 200, 429, 429]


