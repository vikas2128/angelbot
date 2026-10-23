"""Passkey login end to end through the API, with a software passkey and py_webauthn's real verification."""
import json
import os

import pytest
from starlette.testclient import TestClient

from bot import api_auth as AA
from bot import api_server as API
from bot import control
from passkey import SoftKey

RP = "bot.tail1234.ts.net"
ORIGIN = f"https://{RP}"
H = {"origin": ORIGIN, "x-requested-with": "angelbot"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    sent, clock = [], [1_000_000.0]
    store = AA.AuthStore(RP, ORIGIN, folder=tmp_path / "app", notify_fn=sent.append, clock=lambda: clock[0])
    with TestClient(API.create_app(store, tls=True), base_url=ORIGIN) as c:
        yield c, store, sent, clock


def register(c, store, name="Vikas iPhone", key=None):
    key = key or SoftKey(RP, ORIGIN)
    r = c.post("/auth/register/options", json={"code": store.create_enrol_code(name)}, headers=H)
    assert r.status_code == 200, r.text
    r = c.post("/auth/register/verify", json={"id": r.json()["id"], "credential": key.create(r.json()["options"])}, headers=H)
    assert r.status_code == 200, r.text
    return key


def login(c, key):
    r = c.post("/auth/login/options", headers=H).json()
    return c.post("/auth/login/verify", json={"id": r["id"], "credential": key.get(r["options"])}, headers=H)


def test_register_login_read_logout(env):
    c, store, sent, _ = env
    assert c.get("/auth/me").json() == {"signed_in": False, "registered": False}
    key = register(c, store)
    assert [d["name"] for d in store.device_list()] == ["Vikas iPhone"]
    r = login(c, key)
    cookie = r.headers["set-cookie"]
    assert r.status_code == 200 and "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    me = c.get("/auth/me").json()
    assert me["signed_in"] and me["device"] == "Vikas iPhone" and me["stepped_up"]
    assert c.get("/api/status").status_code == 200
    assert c.post("/auth/logout", headers=H).status_code == 200
    assert c.get("/api/status").status_code == 401
    assert any("new device registered" in m for m in sent) and any("enrolment code created" in m for m in sent)


def test_enrolment_code_is_one_use_expiring_and_typo_tolerant(env):
    c, store, _, clock = env
    code = store.create_enrol_code("Phone")
    assert len(code) == 11 and code[5] == "-"
    assert c.post("/auth/register/options", json={"code": code.lower().replace("-", " ")}, headers=H).status_code == 200
    assert c.post("/auth/register/options", json={"code": code}, headers=H).status_code == 401      # used
    code = store.create_enrol_code("Phone")
    clock[0] += AA.ENROL_TTL + 1
    assert c.post("/auth/register/options", json={"code": code}, headers=H).status_code == 401      # expired
    with pytest.raises(AA.AuthError):
        store.create_enrol_code("   ")


def test_lockout_after_failed_attempts_then_it_lifts(env):
    c, store, sent, clock = env
    codes = [c.post("/auth/register/options", json={"code": "WRONG-CODE"}, headers=H).status_code for _ in range(6)]
    assert codes == [401, 401, 401, 401, 401, 429]
    assert any("login locked" in m for m in sent)
    clock[0] += AA.LOCK_SEC + 1
    assert c.post("/auth/register/options", json={"code": "WRONG-CODE"}, headers=H).status_code == 401


def test_forged_or_wrong_passkey_responses_are_refused(env):
    c, store, _, _ = env
    key = register(c, store)
    impostor = SoftKey(RP, ORIGIN)
    impostor.cred_id = key.cred_id                                   # same id, different private key
    r = c.post("/auth/login/options", headers=H).json()
    assert c.post("/auth/login/verify", json={"id": r["id"], "credential": impostor.get(r["options"])}, headers=H).status_code == 401
    r = c.post("/auth/login/options", headers=H).json()
    bad_origin = key.get(r["options"], origin="https://evil.example")
    assert c.post("/auth/login/verify", json={"id": r["id"], "credential": bad_origin}, headers=H).status_code == 401
    r = c.post("/auth/login/options", headers=H).json()
    good = key.get(r["options"])
    assert c.post("/auth/login/verify", json={"id": r["id"], "credential": good}, headers=H).status_code == 200
    assert c.post("/auth/login/verify", json={"id": r["id"], "credential": good}, headers=H).status_code == 401   # replay
    unknown = SoftKey(RP, ORIGIN)
    r = c.post("/auth/login/options", headers=H).json()
    assert c.post("/auth/login/verify", json={"id": r["id"], "credential": unknown.get(r["options"])}, headers=H).status_code == 401


def test_a_passkey_without_face_id_or_pin_is_refused(env):
    c, store, _, _ = env
    r = c.post("/auth/register/options", json={"code": store.create_enrol_code("No UV")}, headers=H).json()
    no_uv = SoftKey(RP, ORIGIN, user_verified=False)
    assert c.post("/auth/register/verify", json={"id": r["id"], "credential": no_uv.create(r["options"])}, headers=H).status_code == 401
    assert store.device_list() == []


def test_csrf_origin_header_and_body_checks(env):
    c, store, _, _ = env
    code = store.create_enrol_code("Phone")
    assert c.post("/auth/register/options", json={"code": code}, headers={"origin": ORIGIN}).status_code == 403
    assert c.post("/auth/register/options", json={"code": code},
                  headers={"origin": "https://evil.example", "x-requested-with": "angelbot"}).status_code == 403
    assert c.post("/auth/register/options", json={"code": code, "admin": True}, headers=H).status_code == 400
    assert c.post("/auth/register/options", content=b"[1,2]", headers=H).status_code == 400
    assert c.post("/auth/register/options", content=b"x" * (API.MAX_BODY + 1), headers=H).status_code == 400


def test_step_up_expires_and_guards_device_removal(env):
    c, store, sent, clock = env
    phone, laptop = register(c, store, "Phone"), register(c, store, "Laptop", SoftKey(RP, ORIGIN))
    assert login(c, phone).status_code == 200
    laptop_id = next(d["id"] for d in store.device_list() if d["name"] == "Laptop")
    clock[0] += AA.STEPUP_TTL + 1
    assert c.get("/auth/me").json()["stepped_up"] is False
    assert c.post("/auth/devices/remove", json={"id": laptop_id}, headers=H).status_code == 403
    r = c.post("/auth/stepup/options", headers=H).json()
    assert [a["id"] for a in r["options"]["allowCredentials"]] == [next(d["id"] for d in store.device_list() if d["name"] == "Phone")]
    wrong = c.post("/auth/stepup/verify", json={"id": r["id"], "credential": laptop.get(r["options"])}, headers=H)
    assert wrong.status_code in (401, 403)                           # another device's passkey cannot step up this session
    r = c.post("/auth/stepup/options", headers=H).json()
    assert c.post("/auth/stepup/verify", json={"id": r["id"], "credential": phone.get(r["options"])}, headers=H).status_code == 200
    assert c.post("/auth/devices/remove", json={"id": laptop_id}, headers=H).json() == {"removed": "Laptop"}
    assert any("device removed" in m for m in sent)


def test_removing_a_device_signs_it_out_and_sessions_expire(env):
    c, store, _, clock = env
    key = register(c, store)
    login(c, key)
    assert c.get("/api/status").status_code == 200
    store.remove_device(store.device_list()[0]["id"], by="the VM")
    assert c.get("/api/status").status_code == 401
    key2 = register(c, store, "Phone 2")
    login(c, key2)
    clock[0] += AA.SESSION_TTL + 1
    assert c.get("/api/status").status_code == 401


def test_websocket_needs_session_and_origin(env, monkeypatch):
    c, store, _, _ = env
    monkeypatch.setattr(API, "WS_PUSH_SEC", 0.05)
    control.write_status({"state": "running", "mode": "paper"})
    with pytest.raises(Exception):
        with c.websocket_connect("/ws", headers={"origin": ORIGIN}) as ws:          # no session
            ws.receive_json()
    login(c, register(c, store))
    # the test client does not send Secure cookies on WebSockets (browsers do), so pass the session cookie by hand
    jar = {"cookie": f"{AA.COOKIE}={c.cookies.get(AA.COOKIE)}"}
    with pytest.raises(Exception):
        with c.websocket_connect("/ws", headers={"origin": "https://evil.example", **jar}) as ws:
            ws.receive_json()
    with c.websocket_connect("/ws", headers={"origin": ORIGIN, **jar}) as ws:
        assert ws.receive_json()["status"]["state"] == "running"


def test_files_are_private_and_hold_no_secrets(env):
    c, store, _, _ = env
    login(c, register(c, store))
    assert oct(store.dir.stat().st_mode & 0o777) == "0o700"
    for name in ("devices.json", "sessions.json", "audit.jsonl"):
        assert oct((store.dir / name).stat().st_mode & 0o777) == "0o600", name
    dev = next(iter(json.loads((store.dir / "devices.json").read_text()).values()))
    assert set(dev) == {"name", "public_key", "sign_count", "created", "last_used"}       # a public key only
    cookie = c.cookies.get(AA.COOKIE)
    assert cookie and cookie not in (store.dir / "sessions.json").read_text()             # sessions are stored hashed
    events = [json.loads(l)["event"] for l in (store.dir / "audit.jsonl").read_text().splitlines()]
    assert events[:3] == ["enrol_code_created", "device_registered", "login"]


def test_signout_all_and_cli(env, monkeypatch, capsys):
    c, store, sent, _ = env
    login(c, register(c, store))
    assert store.signout_all() == 1 and c.get("/api/status").status_code == 401
    assert any("every device signed out" in m for m in sent)
    from bot import cli
    monkeypatch.setattr(AA, "app_dir", lambda: store.dir)
    monkeypatch.setattr(AA, "_telegram", lambda text: None)
    with pytest.raises(SystemExit) as done:
        cli.main(["app", "enroll", "--name", "Tablet"])
    assert done.value.code == 0 and "enrolment code for 'Tablet'" in capsys.readouterr().out
    monkeypatch.setenv("ANGELBOT_PROFILE", "live")
    with pytest.raises(SystemExit, match="only for the paper area"):
        cli.main(["app", "devices"])
