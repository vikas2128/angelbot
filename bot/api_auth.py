"""Login for the app: passkeys only (WebAuthn: Face ID, fingerprint or the phone's PIN). No passwords.

  * A device can register only with a one-time enrolment code made on the VM (`./deploy/ab paper app enroll`):
    valid ENROL_TTL seconds, one use, stored hashed. So being on the tailnet is not enough to add a device.
  * Logging in gives a session (random token, stored hashed, SESSION_TTL long) sent as an HttpOnly, Secure,
    SameSite=Strict cookie. Logging in also counts as a fresh step-up.
  * Step-up: every action (phase 4) needs a passkey check less than STEPUP_TTL old, made in the same session.
  * Lockout: LOCK_AFTER failed checks from one address lock it out for LOCK_SEC; each lockout alerts on Telegram.
  * Everything that matters is written to the audit log (runtime/paper/app/audit.jsonl), and Telegram is told about
    new devices, removed devices, lockouts, new enrolment codes and "sign out everywhere".

Files live in runtime/<area>/app/ (folder 0700, files 0600). Passkeys store only PUBLIC keys: the private key never
leaves the phone.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      ResidentKeyRequirement, UserVerificationRequirement)

from bot import config as C
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.auth")

ENROL_TTL = 600            # an enrolment code works for 10 minutes, once
CHALLENGE_TTL = 120        # a passkey prompt must be answered within 2 minutes
SESSION_TTL = 12 * 3600    # sign in again after 12 hours
STEPUP_TTL = 120           # an action needs a passkey check from the last 2 minutes
LOCK_AFTER = 5             # failed checks from one address ...
LOCK_SEC = 900             # ... lock it out for 15 minutes
COOKIE = "ab_session"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no 0/O, 1/I: easy to type


class AuthError(Exception):
    """Refused. `status` is the HTTP code to send; the message is safe to show."""

    def __init__(self, msg: str, status: int = 401):
        super().__init__(msg)
        self.status = status


def app_dir() -> Path:
    return C.RUNTIME_DIR / "app"


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class AuthStore:
    def __init__(self, rp_id: str, origin: str, folder: Path | None = None, notify_fn=None, clock=time.time):
        self.rp_id, self.origin = rp_id, origin
        self.dir = folder or app_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.notify = notify_fn or _telegram
        self.clock = clock
        self.lock = threading.Lock()
        self.challenges: dict[str, dict] = {}
        self.fails: dict[str, list[float]] = {}
        self.locked_until: dict[str, float] = {}

    # ---------------------------------------------------------------- files
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read(self, name: str) -> dict:
        try:
            return json.loads(self._path(name).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, name: str, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp_")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path(name))

    def audit(self, event: str, ip: str = "", device: str = "", **detail) -> None:
        rec = {"ts": now_ist().isoformat(timespec="seconds"), "event": event, "ip": ip, "device": device, **detail}
        try:
            p = self._path("audit.jsonl")
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.chmod(p, 0o600)
        except OSError as e:
            log.warning("audit write failed: %s", e)
        log.warning("auth %s ip=%s device=%s %s", event, ip, device, detail or "")

    # ---------------------------------------------------------------- enrolment codes (made on the VM)
    def create_enrol_code(self, name: str) -> str:
        name = " ".join(str(name).split())[:40]
        if not name:
            raise AuthError("give the device a name, e.g. --name 'Vikas iPhone'", 400)
        code = "".join(secrets.choice(_ALPHABET) for _ in range(10))
        self._write("enrol.json", {"hash": _h(code), "name": name, "expires": self.clock() + ENROL_TTL})
        self.audit("enrol_code_created", device=name)
        self.notify(f"🔐 <b>App: enrolment code created</b> for “{_esc(name)}” on the VM (valid 10 minutes, one use). "
                    "If this was not you, run <code>./deploy/ab paper app signout-all</code> and check the VM.")
        return f"{code[:5]}-{code[5:]}"

    def _take_code(self, code: str) -> str:
        e = self._read("enrol.json")
        given = str(code or "").replace("-", "").replace(" ", "").upper()
        ok = bool(e.get("hash")) and hmac.compare_digest(_h(given), e["hash"]) and self.clock() <= float(e.get("expires", 0))
        if not ok:
            raise AuthError("that enrolment code is wrong, used or expired: make a new one on the VM")
        self._path("enrol.json").unlink(missing_ok=True)            # one use
        return e["name"]

    # ---------------------------------------------------------------- lockout
    def check_lock(self, ip: str) -> None:
        until = self.locked_until.get(ip, 0)
        if self.clock() < until:
            raise AuthError(f"too many failed attempts; try again in {int((until - self.clock()) // 60) + 1} minutes", 429)

    def _failed(self, ip: str, why: str) -> None:
        now = self.clock()
        q = [t for t in self.fails.get(ip, []) if now - t < LOCK_SEC] + [now]
        self.fails[ip] = q
        self.audit("auth_failed", ip=ip, reason=why)
        if len(q) >= LOCK_AFTER:
            self.locked_until[ip] = now + LOCK_SEC
            self.fails[ip] = []
            self.audit("locked_out", ip=ip, minutes=LOCK_SEC // 60)
            self.notify(f"🚨 <b>App: login locked</b> for {LOCK_SEC // 60} minutes after {LOCK_AFTER} failed attempts "
                        f"from {_esc(ip)}. If this was not you, check your Tailscale devices.")

    # ---------------------------------------------------------------- challenges
    def _challenge(self, kind: str, **extra) -> tuple[str, bytes]:
        cid, challenge = secrets.token_urlsafe(16), secrets.token_bytes(32)
        now = self.clock()
        with self.lock:
            self.challenges = {k: v for k, v in self.challenges.items() if v["expires"] > now}
            self.challenges[cid] = {"kind": kind, "challenge": challenge, "expires": now + CHALLENGE_TTL, **extra}
        return cid, challenge

    def _pop_challenge(self, cid: str, kind: str) -> dict:
        with self.lock:
            c = self.challenges.pop(str(cid or ""), None)
        if not c or c["kind"] != kind or c["expires"] < self.clock():
            raise AuthError("that passkey prompt expired; try again")
        return c

    # ---------------------------------------------------------------- devices
    def devices(self) -> dict:
        return self._read("devices.json")

    def device_list(self) -> list[dict]:
        return [{"id": k, "name": v["name"], "created": v.get("created"), "last_used": v.get("last_used")}
                for k, v in self.devices().items()]

    def remove_device(self, cred_id: str, by: str = "", ip: str = "") -> str:
        with self.lock:
            devs = self.devices()
            d = devs.pop(cred_id, None)
            if d is None:
                raise AuthError("no such device", 404)
            self._write("devices.json", devs)
            sessions = {k: v for k, v in self._read("sessions.json").items() if v.get("device") != cred_id}
            self._write("sessions.json", sessions)
        self.audit("device_removed", ip=ip, device=d["name"], by=by)
        self.notify(f"🔐 <b>App: device removed</b>: “{_esc(d['name'])}” (by {_esc(by or 'the VM')}). It is signed out.")
        return d["name"]

    # ---------------------------------------------------------------- registration
    def register_options(self, code: str, ip: str) -> dict:
        self.check_lock(ip)
        try:
            name = self._take_code(code)
        except AuthError as e:
            self._failed(ip, "bad enrolment code")
            raise e
        cid, challenge = self._challenge("register", name=name)
        opts = generate_registration_options(
            rp_id=self.rp_id, rp_name="AngelBot", user_name="owner", user_id=b"angelbot-owner",
            user_display_name="AngelBot owner", challenge=challenge,
            authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                                   user_verification=UserVerificationRequirement.REQUIRED),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k)) for k in self.devices()])
        return {"id": cid, "options": json.loads(options_to_json(opts))}

    def register_verify(self, cid: str, credential: dict, ip: str) -> dict:
        self.check_lock(ip)
        c = self._pop_challenge(cid, "register")
        try:
            v = verify_registration_response(credential=credential, expected_challenge=c["challenge"],
                                             expected_rp_id=self.rp_id, expected_origin=self.origin,
                                             require_user_verification=True)
        except Exception as e:
            self._failed(ip, f"registration not verified: {type(e).__name__}")
            raise AuthError("the passkey could not be verified") from None
        cred_id = bytes_to_base64url(v.credential_id)
        stamp = now_ist().isoformat(timespec="seconds")
        with self.lock:
            devs = self.devices()
            devs[cred_id] = {"name": c["name"], "public_key": bytes_to_base64url(v.credential_public_key),
                             "sign_count": v.sign_count, "created": stamp, "last_used": None}
            self._write("devices.json", devs)
        self.audit("device_registered", ip=ip, device=c["name"])
        self.notify(f"🔐 <b>App: new device registered</b>: “{_esc(c['name'])}” from {_esc(ip)}. "
                    "If this was not you, remove it: <code>./deploy/ab paper app devices remove</code>")
        return {"id": cred_id, "name": c["name"]}

    # ---------------------------------------------------------------- login and step-up
    def login_options(self, ip: str, session_token: str | None = None) -> dict:
        self.check_lock(ip)
        devs = self.devices()
        if not devs:
            raise AuthError("no device is registered yet: make an enrolment code on the VM", 400)
        sess = self.session(session_token) if session_token else None
        allow = [sess["device"]] if sess else list(devs)               # a step-up must use the session's own passkey
        cid, challenge = self._challenge("login", session=_h(session_token) if sess else None)
        opts = generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge, user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k)) for k in allow])
        return {"id": cid, "options": json.loads(options_to_json(opts))}

    def _verify_assertion(self, cid: str, credential: dict, ip: str) -> tuple[str, dict, dict]:
        self.check_lock(ip)
        c = self._pop_challenge(cid, "login")
        cred_id = str((credential or {}).get("rawId") or (credential or {}).get("id") or "")
        devs = self.devices()
        d = devs.get(cred_id)
        if d is None:
            self._failed(ip, "unknown passkey")
            raise AuthError("this passkey is not registered")
        try:
            v = verify_authentication_response(credential=credential, expected_challenge=c["challenge"],
                                               expected_rp_id=self.rp_id, expected_origin=self.origin,
                                               credential_public_key=base64url_to_bytes(d["public_key"]),
                                               credential_current_sign_count=int(d.get("sign_count") or 0),
                                               require_user_verification=True)
        except Exception as e:
            self._failed(ip, f"login not verified: {type(e).__name__}")
            raise AuthError("the passkey could not be verified") from None
        with self.lock:
            devs = self.devices()
            if cred_id in devs:
                devs[cred_id].update(sign_count=v.new_sign_count, last_used=now_ist().isoformat(timespec="seconds"))
                self._write("devices.json", devs)
        self.fails.pop(ip, None)
        return cred_id, d, c

    def login_verify(self, cid: str, credential: dict, ip: str) -> tuple[str, dict]:
        cred_id, d, c = self._verify_assertion(cid, credential, ip)
        if c.get("session"):
            raise AuthError("that prompt was for a step-up, not a login", 400)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.lock:
            sessions = {k: v for k, v in self._read("sessions.json").items() if v.get("expires", 0) > now}
            sessions[_h(token)] = {"device": cred_id, "name": d["name"], "created": now, "expires": now + SESSION_TTL,
                                   "stepup_at": now, "ip": ip}
            self._write("sessions.json", sessions)
        self.audit("login", ip=ip, device=d["name"])
        return token, {"name": d["name"], "expires": now + SESSION_TTL}

    def stepup_verify(self, session_token: str, cid: str, credential: dict, ip: str) -> None:
        sess = self.session(session_token)
        if not sess:
            raise AuthError("signed out")
        cred_id, d, c = self._verify_assertion(cid, credential, ip)
        if c.get("session") != _h(session_token) or cred_id != sess["device"]:
            self._failed(ip, "step-up with another session or device")
            raise AuthError("use the passkey of this device", 403)
        with self.lock:
            sessions = self._read("sessions.json")
            if _h(session_token) in sessions:
                sessions[_h(session_token)]["stepup_at"] = self.clock()
                self._write("sessions.json", sessions)
        self.audit("step_up", ip=ip, device=d["name"])

    # ---------------------------------------------------------------- sessions
    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self._read("sessions.json").get(_h(token))
        if not s or s.get("expires", 0) <= self.clock() or s.get("device") not in self.devices():
            return None
        return s

    def stepped_up(self, token: str | None) -> bool:
        s = self.session(token)
        return bool(s) and self.clock() - float(s.get("stepup_at", 0)) <= STEPUP_TTL

    def logout(self, token: str | None, ip: str = "") -> None:
        if not token:
            return
        with self.lock:
            sessions = self._read("sessions.json")
            s = sessions.pop(_h(token), None)
            self._write("sessions.json", sessions)
        if s:
            self.audit("logout", ip=ip, device=s.get("name", ""))

    def signout_all(self, by: str = "the VM") -> int:
        with self.lock:
            n = len(self._read("sessions.json"))
            self._write("sessions.json", {})
        self.audit("signout_all", by=by, sessions=n)
        self.notify(f"🔐 <b>App: every device signed out</b> ({n} session(s)), by {_esc(by)}.")
        return n


def _esc(s: str) -> str:
    import html
    return html.escape(str(s))


def _telegram(text: str) -> None:
    from bot import notify
    notify.send(text)
