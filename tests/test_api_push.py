"""Phase 6: web push. Real RFC 8291 encryption and VAPID signing; only the network call is replaced, and the test
decrypts what would have been sent, as a phone would."""
import base64
import json
import os
from types import SimpleNamespace

import http_ece
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from bot import alerts, api_auth as AA, api_push as AP, api_server as API
from test_api_auth import H, ORIGIN, RP, login, register

FCM = "https://fcm.googleapis.com/fcm/send/abc123"


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Phone:
    """A browser's push subscription: its own key pair and auth secret."""
    def __init__(self, endpoint=FCM):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        pub = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.sub = {"endpoint": endpoint, "keys": {"p256dh": b64(pub), "auth": b64(self.auth)}}

    def read(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))


class FakeSession:
    def __init__(self, status=201):
        self.status, self.posts = status, []

    def post(self, url, data=None, headers=None, timeout=None, **kw):
        self.posts.append(SimpleNamespace(url=url, data=data, headers=headers))
        return SimpleNamespace(status_code=self.status, text="", headers={}, reason="Gone" if self.status == 410 else "")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    alerts.path().unlink(missing_ok=True)
    store = AA.AuthStore(RP, ORIGIN, folder=tmp_path / "app", notify_fn=lambda t: None)
    session = FakeSession()
    pusher = AP.Pusher(store, session=session)
    with TestClient(API.create_app(store, tls=True, pusher=pusher), base_url=ORIGIN) as c:
        key = register(c, store, "Phone")
        login(c, key)
        yield SimpleNamespace(c=c, store=store, pusher=pusher, session=session,
                              device=store.device_list()[0]["id"])


def test_only_real_browser_push_services_are_accepted():
    ok = ["https://fcm.googleapis.com/fcm/send/x", "https://web.push.apple.com/QG", "https://api.push.apple.com/x",
          "https://updates.push.services.mozilla.com/wpush/v2/x", "https://wns2-par02p.notify.windows.com/w/?token=x"]
    bad = ["http://fcm.googleapis.com/x", "https://fcm.googleapis.com.evil.com/x", "https://evil.com/fcm.googleapis.com",
           "https://169.254.169.254/latest", "https://push.apple.com.attacker.net/x", "https://localhost/x", "ftp://x", "", None]
    assert all(AP.allowed_endpoint(u) for u in ok) and not any(AP.allowed_endpoint(u) for u in bad)


def test_push_is_encrypted_and_signed_and_readable_by_the_phone(env):
    phone = Phone()
    env.pusher.subscribe(phone.sub, env.device, "Phone")
    r = env.pusher.send("📝 PAPER · Bought TCS-EQ", "5 shares at ₹2,571.40")
    assert r == {"sent": 1, "dropped": 0, "failed": 0}
    post = env.session.posts[0]
    assert post.url == FCM and post.headers["Content-Encoding"] == "aes128gcm" and int(post.headers["TTL"]) == AP.TTL_SEC
    assert b"Bought" not in post.data                                         # the push service sees only ciphertext
    msg = phone.read(post.data)
    assert msg["title"] == "📝 PAPER · Bought TCS-EQ" and msg["body"] == "5 shares at ₹2,571.40"
    auth = post.headers["Authorization"]                                     # VAPID: signed for this push service only
    assert auth.startswith("vapid t=") and f"k={env.pusher.public_key()}" in auth
    claims = json.loads(unb64(auth.split("t=")[1].split(",")[0].split(".")[1]))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == f"https://{RP}"   # a real contact
    import time
    assert 0 < claims["exp"] - time.time() <= 1800 + 5                       # short-lived token


def test_gone_and_orphaned_subscriptions_are_dropped(env):
    a, b = Phone(FCM + "a"), Phone(FCM + "b")
    env.pusher.subscribe(a.sub, env.device, "Phone")
    env.pusher.subscribe(b.sub, env.device, "Phone")
    env.session.status = 410
    assert env.pusher.send("t", "b") == {"sent": 0, "dropped": 2, "failed": 0} and not env.pusher.device_has(env.device)
    env.session.status = 201
    env.pusher.subscribe(a.sub, env.device, "Phone")
    before = len(env.session.posts)
    env.store.remove_device(env.device)                                       # device removed: its pushes stop
    assert env.pusher.send("t", "b") == {"sent": 0, "dropped": 1, "failed": 0} and len(env.session.posts) == before
    env.session.status = 500
    assert env.pusher.send("t", "b") == {"sent": 0, "dropped": 0, "failed": 0}


def test_outbox_watcher_sends_only_new_alerts(env):
    phone = Phone()
    env.pusher.subscribe(phone.sub, env.device, "Phone")
    alerts.append("<b>old</b>", "old alert", "📝 PAPER")
    env.pusher.skip_backlog()                                                  # as at start-up: no re-sends
    assert env.pusher.check_outbox() == 0 and env.session.posts == []
    alerts.append("🟢 <b>Bought TCS-EQ</b>\n5 shares", "🟢 Bought TCS-EQ\n5 shares", "📝 PAPER")
    assert env.pusher.check_outbox() == 1
    msg = phone.read(env.session.posts[0].data)
    assert msg["title"] == "📝 PAPER · 🟢 Bought TCS-EQ" and msg["body"] == "5 shares"
    assert env.pusher.check_outbox() == 0                                      # nothing new
    alerts.path().write_text("")                                               # trimmed or replaced: start again from its end
    assert env.pusher.check_outbox() == 0


def test_api_subscribe_test_and_unsubscribe(env):
    c = env.c
    assert len(c.get("/api/push/key").json()["key"]) == 87                     # 65-byte P-256 public key, base64url
    phone = Phone()
    assert c.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://evil.com/x", "keys": phone.sub["keys"]}},
                  headers=H).status_code == 400
    assert c.post("/api/push/subscribe", json={"subscription": {"endpoint": FCM}}, headers=H).status_code == 400
    assert c.post("/api/push/subscribe", json={"subscription": phone.sub, "extra": 1}, headers=H).status_code == 400
    assert c.post("/api/push/subscribe", json={"subscription": phone.sub}, headers={"origin": ORIGIN}).status_code == 403
    assert c.post("/api/push/subscribe", json={"subscription": phone.sub}, headers=H).json() == {"subscribed": True}
    assert c.get("/auth/me").json()["push"] is True
    r = c.post("/api/push/test", headers=H).json()
    assert r["sent"] == 1 and "Notifications work on Phone" in phone.read(env.session.posts[-1].data)["body"]
    assert c.post("/api/push/unsubscribe", json={"endpoint": "https://fcm.googleapis.com/other"}, headers=H).json() == {"unsubscribed": False}
    assert c.post("/api/push/unsubscribe", json={"endpoint": FCM}, headers=H).json() == {"unsubscribed": True}
    c.cookies.clear()
    assert c.get("/api/push/key").status_code == 401 and c.post("/api/push/test", headers=H).status_code == 401


def test_a_device_keeps_only_its_newest_subscriptions_and_files_are_private(env):
    for i in range(5):
        env.pusher.subscribe(Phone(f"{FCM}{i}").sub, env.device, "Phone")
    subs = json.loads((env.store.dir / "push.json").read_text())
    assert sorted(subs) == [f"{FCM}{i}" for i in (2, 3, 4)]
    env.pusher.public_key()
    for name in ("push.json", "vapid.pem"):
        assert oct((env.store.dir / name).stat().st_mode & 0o777) == "0o600", name


def test_a_refusal_reaches_the_app_with_its_reason(env, monkeypatch):
    phone = Phone("https://web.push.apple.com/QGabc")
    env.pusher.subscribe(phone.sub, env.device, "Phone")
    env.session.status = 403
    env.session.posts.clear()
    orig = env.session.post

    def refuse(url, **kw):
        r = orig(url, **kw)
        r.text = '{"reason":"BadJwtToken"}'
        r.reason = "Forbidden"
        return r
    monkeypatch.setattr(env.session, "post", refuse)
    r = env.c.post("/api/push/test", headers=H).json()
    assert r["sent"] == 0 and r["failed"] == 1 and "web.push.apple.com answered 403" in r["errors"][0]
    assert "BadJwtToken" in r["errors"][0] and env.pusher.device_has(env.device)     # kept: 403 is not "gone"


def test_contact_can_be_set(monkeypatch):
    from types import SimpleNamespace
    store = SimpleNamespace(rp_id="localhost", dir=None)
    assert AP.Pusher(store).contact == "mailto:angelbot@example.com"
    monkeypatch.setenv("ANGELBOT_PUSH_CONTACT", "mailto:me@gmail.com")
    assert AP.Pusher(store).contact == "mailto:me@gmail.com"
