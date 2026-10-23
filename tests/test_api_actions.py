"""Phase 4: the app's actions. Each needs a session, the app's origin and CSRF header, and a fresh step-up, and goes
through bot/actions.py with the same rules as Telegram."""
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from bot import api_auth as AA
from bot import api_server as API
from bot import control
from bot import settings as ST
from test_api_auth import H, ORIGIN, RP, login, register

ACTIONS = ["/api/control/pause", "/api/control/resume", "/api/control/kill/ask", "/api/control/kill/confirm",
           "/api/control/restart", "/api/settings/set", "/api/settings/reset"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    for p in (control.STATUS, control.CONTROL, ST.path()):
        p.unlink(missing_ok=True)
    sent, clock, calls = [], [1_000_000.0], []
    store = AA.AuthStore(RP, ORIGIN, folder=tmp_path / "app", notify_fn=sent.append, clock=lambda: clock[0])
    runner = lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stderr="")
    with TestClient(API.create_app(store, tls=True, restart_runner=runner), base_url=ORIGIN) as c:
        key = register(c, store, "Phone")
        assert login(c, key).status_code == 200                 # a login is also a fresh step-up
        yield SimpleNamespace(c=c, store=store, sent=sent, clock=clock, calls=calls, key=key)
    for p in (control.STATUS, control.CONTROL, ST.path()):
        p.unlink(missing_ok=True)


def step_up(e):
    r = e.c.post("/auth/stepup/options", headers=H).json()
    assert e.c.post("/auth/stepup/verify", json={"id": r["id"], "credential": e.key.get(r["options"])}, headers=H).status_code == 200


def test_every_action_needs_session_csrf_and_a_fresh_step_up(env):
    c = env.c
    for path in ACTIONS:
        assert c.post(path, json={}, headers={"origin": ORIGIN}).status_code == 403, path          # no CSRF header
    env.clock[0] += AA.STEPUP_TTL + 1
    for path in ACTIONS:
        r = c.post(path, json={}, headers=H)
        assert r.status_code == 403 and "passkey" in r.json()["error"], path                       # stale step-up
    c.cookies.clear()
    for path in ACTIONS:
        assert c.post(path, json={}, headers=H).status_code == 401, path                           # signed out
    for path in ACTIONS:
        assert c.get(path).status_code == 405, path                                                # never by GET


def test_pause_and_resume_write_the_same_control_file_as_telegram(env):
    r = env.c.post("/api/control/pause", headers=H)
    assert r.status_code == 200 and control.read_control()["paused"] is True
    assert control.read_json(control.CONTROL)["note"] == "app: Phone pause"
    env.c.post("/api/control/resume", headers=H)
    c = control.read_control()
    assert c["paused"] is False and c["kill"] is False


def test_emergency_stop_is_two_step_and_expires(env, monkeypatch):
    c = env.c
    assert c.post("/api/control/kill/confirm", json={"confirm": "guess"}, headers=H).status_code == 409   # never asked
    ask = c.post("/api/control/kill/ask", headers=H).json()
    assert "closes ALL positions" in ask["warning"] and control.read_control()["kill"] is False
    assert c.post("/api/control/kill/confirm", json={"confirm": "wrong"}, headers=H).status_code == 409
    assert control.read_control()["kill"] is False                                                     # a wrong one uses it up
    ask = c.post("/api/control/kill/ask", headers=H).json()
    monkeypatch.setattr(API.time, "monotonic", lambda: 10**12)                                          # a minute later
    assert c.post("/api/control/kill/confirm", json={"confirm": ask["confirm"]}, headers=H).status_code == 409
    monkeypatch.undo()
    ask = c.post("/api/control/kill/ask", headers=H).json()
    r = c.post("/api/control/kill/confirm", json={"confirm": ask["confirm"]}, headers=H)
    assert r.status_code == 200 and control.read_control()["kill"] is True
    assert any("Emergency stop from the app" in m for m in env.sent)


def test_restart_uses_the_same_refusals_as_telegram(env, monkeypatch):
    c = env.c
    r = c.post("/api/control/restart", headers=H)
    assert r.status_code == 409 and "isn't running" in r.json()["error"] and env.calls == []
    control.write_status({"state": "running", "replay": False, "positions": [{"symbol": "TCS-EQ", "qty": 5}]})
    assert "open position" in c.post("/api/control/restart", headers=H).json()["error"] and env.calls == []
    control.write_status({"state": "running", "replay": False, "positions": [], "pending_orders": []})
    assert c.post("/api/control/restart", headers=H).json() == {"done": True}
    assert env.calls == [["systemctl", "--user", "--no-block", "restart", "angelbot@paper.service"]]
    assert any("restart from the app" in m for m in env.sent)
    monkeypatch.setenv("ANGELBOT_PROFILE", "live")
    assert "only for the paper area" in c.post("/api/control/restart", headers=H).json()["error"]


def test_settings_through_the_same_choices_and_checks(env, monkeypatch):
    monkeypatch.setattr(API, "ACTIONS_PER_MIN", 50)          # refused attempts count towards the limit too
    c = env.c
    i = ST.BY_ID["maxtr"]["choices"].index(5)
    r = c.post("/api/settings/set", json={"id": "maxtr", "choice": i}, headers=H).json()
    assert r["done"] and r["new"] == "5" and ST.overrides()["risk.max_trades_per_day"] == 5
    assert c.post("/api/settings/set", json={"id": "amount", "amount": "7,500"}, headers=H).json()["new"].startswith("₹7,500")
    for bad, why in (({"id": "maxtr", "choice": 99}, "Unknown choice"), ({"id": "nope", "choice": 0}, "Unknown setting"),
                     ({"id": "amount", "amount": "nan"}, "Not a number"), ({"id": "amount", "amount": 900000}, "above"),
                     ({"id": "maxtr", "choice": "1"}, "whole number"), ({"id": "maxtr", "choice": True}, "whole number"),
                     ({"id": "maxtr"}, "exactly one"), ({"id": "maxtr", "choice": 0, "amount": 1}, "exactly one"),
                     ({"id": "maxtr", "choice": 0, "path": "risk.max_daily_loss"}, "unexpected field")):
        r = c.post("/api/settings/set", json=bad, headers=H)
        assert r.status_code == 400 and why in r.json()["error"], (bad, r.json())
    assert c.post("/api/settings/reset", headers=H).json() == {"done": True, "reset": 2} and ST.overrides() == {}


def test_actions_are_rate_limited_per_device_and_audited(env, monkeypatch):
    monkeypatch.setattr(API, "ACTIONS_PER_MIN", 3)
    with TestClient(API.create_app(env.store, tls=True), base_url=ORIGIN) as c:
        c.cookies.update(env.c.cookies)
        codes = [c.post("/api/control/pause", headers=H).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    events = [l for l in (env.store.dir / "audit.jsonl").read_text().splitlines() if "action_pause" in l]
    assert len(events) == 3 and '"device": "Phone"' in events[0]


def test_step_up_window_lets_a_few_actions_through_then_asks_again(env):
    c = env.c
    env.clock[0] += AA.STEPUP_TTL - 5
    assert c.post("/api/control/pause", headers=H).status_code == 200
    env.clock[0] += 10
    assert c.post("/api/control/resume", headers=H).status_code == 403
    step_up(env)
    assert c.post("/api/control/resume", headers=H).status_code == 200
