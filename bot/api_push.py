"""Web push for the app: every bot alert (from the outbox, bot/alerts.py) becomes a notification on each registered
device that turned notifications on.

  * VAPID keys are made on first use in runtime/<area>/app/vapid.pem (0600). The app gets only the public key.
  * Subscriptions are kept per device in runtime/<area>/app/push.json (0600). A subscription whose device was removed
    is dropped; one the push service says is gone (404 / 410) is dropped too.
  * The server only ever posts to the browsers' own push services (Google, Apple, Mozilla, Microsoft), never to an
    address a request names: an endpoint on any other host is refused when subscribing.
  * Messages are encrypted end to end (RFC 8291, aes128gcm) by pywebpush: the push service cannot read them. They hold
    the alert's plain text only, cut to a short length.
  * A watcher thread in the API reads new outbox lines every PUSH_POLL_SEC and sends them. It starts at the end of the
    outbox, so restarting the API never re-sends old alerts.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from bot import alerts

log = logging.getLogger("angelbot.push")

PUSH_POLL_SEC = 2.0
MAX_BODY = 600
MAX_SUBS_PER_DEVICE = 3
TTL_SEC = 3600                      # an alert older than an hour is not worth delivering
PUSH_HOSTS = ("fcm.googleapis.com", ".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")


class PushError(ValueError):
    pass


def allowed_endpoint(url: str) -> bool:
    try:
        u = urlparse(str(url))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


class Pusher:
    def __init__(self, store, folder: Path | None = None, contact: str = "mailto:owner@angelbot.invalid",
                 send_fn=None, session=None):
        self.store = store                       # the AuthStore: which devices still exist
        self.dir = folder or store.dir
        self.contact = contact
        self.send_fn = send_fn                   # tests replace the network call
        self.session = session
        self.lock = threading.Lock()
        self._vapid = None
        self._seen = 0                           # outbox lines already handled
        self._stop = threading.Event()

    # ---------------------------------------------------------------- keys
    def vapid(self):
        from py_vapid import Vapid02
        if self._vapid is None:
            p = self.dir / "vapid.pem"
            if p.exists():
                self._vapid = Vapid02.from_file(str(p))
            else:
                v = Vapid02()
                v.generate_keys()
                fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp_")
                os.close(fd)
                v.save_key(tmp)
                os.chmod(tmp, 0o600)
                os.replace(tmp, p)
                self._vapid = v
                log.warning("created push keys in %s", p)
        return self._vapid

    def public_key(self) -> str:
        import base64
        from cryptography.hazmat.primitives import serialization
        raw = self.vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    # ---------------------------------------------------------------- subscriptions
    def _read(self) -> dict:
        try:
            return json.loads((self.dir / "push.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, subs: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp_")
        with os.fdopen(fd, "w") as f:
            json.dump(subs, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.dir / "push.json")

    def subscribe(self, sub: dict, device_id: str, device_name: str) -> None:
        if not isinstance(sub, dict):
            raise PushError("subscription must be an object")
        endpoint, keys = sub.get("endpoint"), sub.get("keys") or {}
        if not isinstance(endpoint, str) or len(endpoint) > 1000 or not allowed_endpoint(endpoint):
            raise PushError("that push address is not from a known browser push service")
        if not isinstance(keys, dict) or not all(isinstance(keys.get(k), str) and 0 < len(keys[k]) < 200 for k in ("p256dh", "auth")):
            raise PushError("subscription keys are missing")
        with self.lock:
            subs = self._read()
            if endpoint not in subs:               # keep a device's newest few (a phone that re-subscribes leaves old ones)
                mine = sorted((k for k, v in subs.items() if v.get("device") == device_id), key=lambda k: subs[k]["created"])
                for old in mine[:max(0, len(mine) - MAX_SUBS_PER_DEVICE + 1)]:
                    subs.pop(old, None)
            subs[endpoint] = {"keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}, "device": device_id,
                              "name": device_name, "created": time.time()}
            self._write(subs)
        self.store.audit("push_subscribed", device=device_name)

    def unsubscribe(self, endpoint: str, device_id: str) -> bool:
        with self.lock:
            subs = self._read()
            s = subs.get(str(endpoint))
            if not s or s.get("device") != device_id:
                return False
            subs.pop(endpoint)
            self._write(subs)
        return True

    def device_has(self, device_id: str) -> bool:
        return any(v.get("device") == device_id for v in self._read().values())

    # ---------------------------------------------------------------- sending
    def _push_one(self, endpoint: str, sub: dict, payload: str) -> int:
        from pywebpush import WebPushException, webpush
        info = {"endpoint": endpoint, "keys": sub["keys"]}
        aud = f"{urlparse(endpoint).scheme}://{urlparse(endpoint).netloc}"
        try:
            r = webpush(info, data=payload, vapid_private_key=self.vapid(),
                        vapid_claims={"sub": self.contact, "aud": aud}, ttl=TTL_SEC, timeout=10,
                        requests_session=self.session)
            return getattr(r, "status_code", 201)
        except WebPushException as e:
            return getattr(e.response, "status_code", 0) if e.response is not None else 0

    def send(self, title: str, body: str, only_device: str | None = None) -> dict:
        """Push to every subscription (or one device's). Drops gone and orphaned subscriptions. Never raises."""
        payload = json.dumps({"title": title[:80], "body": body[:MAX_BODY], "ts": int(time.time())}, ensure_ascii=False)
        devices = self.store.devices()
        sent = dropped = failed = 0
        with self.lock:
            subs = self._read()
        for endpoint, sub in list(subs.items()):
            if only_device and sub.get("device") != only_device:
                continue
            if sub.get("device") not in devices or not allowed_endpoint(endpoint):
                subs.pop(endpoint, None)
                dropped += 1
                continue
            try:
                status = (self.send_fn or self._push_one)(endpoint, sub, payload)
            except Exception as e:                                # a push problem must never stop the watcher
                log.warning("push failed: %s", e)
                status = 0
            if status in (404, 410):
                subs.pop(endpoint, None)
                dropped += 1
            elif 200 <= status < 300:
                sent += 1
            else:
                failed += 1
        if dropped:
            with self.lock:
                current = self._read()
                for k in list(current):
                    if k not in subs:
                        current.pop(k)
                self._write(current)
        return {"sent": sent, "dropped": dropped, "failed": failed}

    # ---------------------------------------------------------------- outbox watcher
    def skip_backlog(self) -> None:
        self._seen = len(_lines())

    def check_outbox(self) -> int:
        """Send alerts added to the outbox since the last check. Returns how many."""
        lines = _lines()
        if len(lines) < self._seen:              # the outbox was trimmed: start from its new end
            self._seen = len(lines)
            return 0
        new, self._seen = lines[self._seen:], len(lines)
        n = 0
        for line in new:
            try:
                a = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = str(a.get("text") or "").strip()
            title, _, rest = text.partition("\n")
            label = str(a.get("label") or "").strip()
            self.send(f"{label} · {title}" if label else title, rest or title)
            n += 1
        return n

    def start(self) -> None:
        self.skip_backlog()

        def loop():
            while not self._stop.wait(PUSH_POLL_SEC):
                try:
                    self.check_outbox()
                except Exception as e:
                    log.warning("push watcher error: %s", e)
        threading.Thread(target=loop, name="push", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def _lines() -> list[str]:
    try:
        return alerts.path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
