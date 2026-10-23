"""The app's API: read-only data, passkey login, and the same few actions Telegram has (phases 2-4). Serves status, trades, report, screener, settings and alerts for the PAPER area,
plus a WebSocket with live status. See docs/APP_PLAN.md.

Safety, by construction:
  * Data routes are GET only. The only changes are the ones Telegram can make, through bot/actions.py: pause,
    resume, emergency stop (asked, then confirmed within a minute), restarting the paper bot (same safety checks)
    and settings (same fixed choices and validation). Each needs a passkey check from the last 2 minutes, and at
    most 10 a minute per device. There is no route that places, modifies or cancels orders, switches paper/live,
    or reads .env, profiles or broker credentials. The API never logs in to the broker.
  * Area-locked: it refuses to run for any area but paper, and reads only that area's runtime folder.
  * Network-locked: it binds only to a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48) with HTTPS, or, with
    --dev, to 127.0.0.1 for local testing. It never binds to 0.0.0.0.
  * Login with passkeys only (bot/api_auth.py): a device registers with a one-time code made on the VM; reads need a
    session cookie; every POST needs the app's own origin and an X-Requested-With header; the WebSocket checks both
    the origin and the session at connect.

    ./deploy/ab paper api --host 100.101.102.103 --name <vm>.<tailnet>.ts.net --cert <crt> --key <key>   (on the VM)
    ./deploy/ab paper api --dev                                     (local test: 127.0.0.1, open http://localhost:8443)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import secrets
import subprocess
import ipaddress
import logging
import os
import re
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from bot import actions as A
from bot import alerts, control
from bot import config as C
from bot import settings as ST
from bot.api_auth import COOKIE, SESSION_TTL, AuthError, AuthStore, _esc
from bot.journal import query_readonly
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.api")

READS_PER_MIN = 30
ACTIONS_PER_MIN = 10
KILL_CONFIRM_SEC = 60
WS_PUSH_SEC = 2.0
TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class StartupError(SystemExit):
    pass


class BadRequest(ValueError):
    """The request itself is wrong (HTTP 400). Any other ValueError means there is nothing to show yet (404)."""


# ───────────────────────────── where and how it may run ─────────────────────────────
def check_area() -> str:
    area = os.environ.get("ANGELBOT_PROFILE") or ""
    if area != "paper":
        raise StartupError(f"the app API runs only for the paper area (ANGELBOT_PROFILE={area!r}); "
                           "start it with ./deploy/ab paper ...")
    return area


def check_bind(host: str, dev: bool) -> None:
    """Only a Tailscale address (or 127.0.0.1 with --dev). Never 0.0.0.0 or a public address."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise StartupError(f"--host must be an IP address, not {host!r}") from None
    if dev and ip.is_loopback:
        return
    if not any(ip in n for n in TAILSCALE_NETS):
        raise StartupError(f"refusing to listen on {host}: only a Tailscale address (100.64.0.0/10) is allowed "
                           "(find it with: tailscale ip -4), or 127.0.0.1 with --dev")


# ───────────────────────────── data (read-only) ─────────────────────────────
def status_payload() -> dict:
    s = control.read_status()
    s["control"] = control.read_control()
    return s


def trades_payload(date: str | None) -> dict:
    if date and not _DATE.match(date):
        raise BadRequest("date must be YYYY-MM-DD")
    day = date or now_ist().strftime("%Y-%m-%d")
    if not control.JOURNAL.exists():
        return {"date": day, "trades": [], "totals": _totals([])}
    rows = query_readonly(control.JOURNAL,
                          "SELECT symbol, side, qty, entry_ts, entry_price, exit_ts, exit_price, gross_pnl, charges, "
                          f"net_pnl, exit_reason, strategy, mode FROM trades WHERE substr(exit_ts,1,10)='{day}' "
                          "ORDER BY exit_ts", 500)
    return {"date": day, "trades": rows, "totals": _totals(rows)}


def _totals(rows: list[dict]) -> dict:
    net = sum(float(r.get("net_pnl") or 0) for r in rows)
    wins = sum(1 for r in rows if float(r.get("net_pnl") or 0) > 0)
    return {"trades": len(rows), "net_pnl": round(net, 2),
            "charges": round(sum(float(r.get("charges") or 0) for r in rows), 2),
            "gross_pnl": round(sum(float(r.get("gross_pnl") or 0) for r in rows), 2),
            "win_rate_pct": round(100 * wins / len(rows), 1) if rows else 0.0}


def settings_payload() -> dict:
    cfg = ST.load()
    vals = ST.overrides()
    items = []
    for s in ST.SETTINGS:
        cur = ST.current(cfg, s)
        items.append({"id": s["id"], "label": s["label"], "value": ST.show(s, cur), "changed": s["path"] in vals,
                      "choices": [{"index": i, "label": ST.show(s, c), "current": c == cur} for i, c in enumerate(s["choices"])],
                      "typed": bool(s.get("typed"))})
    return {"area": os.environ.get("ANGELBOT_PROFILE"), "settings": items, "problems": ST.problems()}


def _report_html(arg: str) -> tuple[str, str]:
    from bot.telegram_ctl import build_report_file
    with tempfile.TemporaryDirectory() as d:
        p, caption = build_report_file(arg, Path(d))
        return p.read_text(encoding="utf-8"), caption


def _screener_html() -> tuple[str, str]:
    from bot.telegram_ctl import build_screen_file
    with tempfile.TemporaryDirectory() as d:
        p, summary = build_screen_file(Path(d))
        return p.read_text(encoding="utf-8"), summary


# ───────────────────────────── HTTP ─────────────────────────────
MAX_BODY = 64 * 1024
CSRF_HEADER = ("x-requested-with", "angelbot")


def create_app(store: AuthStore, tls: bool = True, restart_runner=subprocess.run) -> Starlette:
    """`store` holds devices and sessions (bot.api_auth). Reads need a session; actions need a session, the right
    Origin, the X-Requested-With header (CSRF) and a fresh step-up (a passkey check from the last 2 minutes)."""
    hits: dict[str, deque] = defaultdict(deque)
    acts: dict[str, deque] = defaultdict(deque)
    kill_asks: dict[str, tuple[str, float]] = {}       # session hash -> (confirmation token, expiry)

    def ip_of(request) -> str:
        return request.client.host if request.client else "?"

    def rate_ok(ip: str) -> bool:
        q, now = hits[ip], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= READS_PER_MIN:
            return False
        q.append(now)
        return True

    def action_ok(who: str) -> bool:
        q, now = acts[who], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= ACTIONS_PER_MIN:
            return False
        q.append(now)
        return True

    def secure(resp: Response) -> Response:
        resp.headers.update(SECURITY_HEADERS)
        if tls:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def err(msg: str, status: int) -> Response:
        return secure(JSONResponse({"error": msg}, status_code=status))

    def same_origin_post(request: Request) -> bool:
        return (request.headers.get("origin") == store.origin
                and request.headers.get(CSRF_HEADER[0], "").lower() == CSRF_HEADER[1])

    async def body_json(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise BadRequest("request too large")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise BadRequest("the body must be JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("the body must be a JSON object")
        return data

    def endpoint(fn):
        """A read: needs a valid session."""
        async def handler(request: Request) -> Response:
            if not store.session(request.cookies.get(COOKIE)):
                return err("signed out", 401)
            if not rate_ok(ip_of(request)):
                return err("too many requests, try again in a minute", 429)
            try:
                return secure(await asyncio.to_thread(fn, request))
            except ValueError as e:                    # bad input, or nothing to show yet: safe to tell the user
                return err(str(e), 400 if isinstance(e, BadRequest) else 404)
            except Exception:
                log.exception("api error on %s", request.url.path)
                return err("internal error", 500)
        return handler

    def auth_post(fn, need_session: bool = False, need_stepup: bool = False, allowed: tuple = ()):
        """A login step or an action: POST, same origin, CSRF header, JSON object body with only `allowed` keys."""
        async def handler(request: Request) -> Response:
            if not same_origin_post(request):
                return err("refused: wrong origin", 403)
            ip, token = ip_of(request), request.cookies.get(COOKIE)
            if not rate_ok(ip):
                return err("too many requests, try again in a minute", 429)
            if need_session and not store.session(token):
                return err("signed out", 401)
            if need_stepup and not store.stepped_up(token):
                return err("confirm with your passkey first", 403)
            try:
                data = await body_json(request)
                extra = set(data) - set(allowed)
                if extra:
                    raise BadRequest(f"unexpected field(s): {', '.join(sorted(extra))}")
                return secure(await asyncio.to_thread(fn, data, ip, token))
            except AuthError as e:
                return err(str(e), e.status)
            except ValueError as e:
                return err(str(e), 400)
            except Exception:
                log.exception("auth error on %s", request.url.path)
                return err("internal error", 500)
        return handler

    # ---- reads
    def status(_r):
        return JSONResponse(status_payload())

    def trades(r):
        return JSONResponse(trades_payload(r.query_params.get("date")))

    def settings(_r):
        return JSONResponse(settings_payload())

    def alerts_(r):
        limit = r.query_params.get("limit", "50")
        if not limit.isdigit():
            raise BadRequest("limit must be a number")
        return JSONResponse({"alerts": alerts.read(r.query_params.get("since"), int(limit))})

    def report(r):
        page, caption = _report_html(" ".join(x for x in (r.query_params.get("source"), r.query_params.get("since")) if x))
        return HTMLResponse(page, headers={"X-Caption": re.sub(r"[^\x20-\x7e]", "", caption)[:200]})

    def screener(_r):
        page, _summary = _screener_html()
        return HTMLResponse(page)

    def devices(_r):
        return JSONResponse({"devices": store.device_list()})

    async def health(_r):
        return secure(JSONResponse({"ok": True}))        # no details: says only that the service answers

    async def me(request: Request):
        s = store.session(request.cookies.get(COOKIE))
        if not s:
            return secure(JSONResponse({"signed_in": False, "registered": bool(store.devices())}))
        return secure(JSONResponse({"signed_in": True, "device": s["name"], "expires": s["expires"],
                                    "stepped_up": store.stepped_up(request.cookies.get(COOKIE))}))

    # ---- login steps
    def reg_options(data, ip, _t):
        return JSONResponse(store.register_options(data.get("code", ""), ip))

    def reg_verify(data, ip, _t):
        return JSONResponse({"registered": store.register_verify(data.get("id", ""), data.get("credential") or {}, ip)})

    def login_options(_d, ip, _t):
        return JSONResponse(store.login_options(ip))

    def login_verify(data, ip, _t):
        token, info = store.login_verify(data.get("id", ""), data.get("credential") or {}, ip)
        resp = JSONResponse({"signed_in": True, **info})
        resp.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, secure=tls, samesite="strict", path="/")
        return resp

    def stepup_options(_d, ip, token):
        return JSONResponse(store.login_options(ip, session_token=token))

    def stepup_verify(data, ip, token):
        store.stepup_verify(token, data.get("id", ""), data.get("credential") or {}, ip)
        return JSONResponse({"stepped_up": True})

    def logout(_d, ip, token):
        store.logout(token, ip)
        resp = JSONResponse({"signed_in": False})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---- actions (phase 4): every one needs a session and a fresh step-up; all go through bot/actions.py
    def who(token) -> str:
        s = store.session(token)
        return s["name"] if s else "?"

    def act(fn):
        """Per-device action limit, then the action, then an audit record. `fn(data, ip, token, device)`."""
        def run(data, ip, token):
            device = who(token)
            if not action_ok(device):
                raise AuthError("too many actions, try again in a minute", 429)
            return fn(data, ip, token, device)
        return run

    def pause(_d, ip, _t, device):
        c = A.pause(f"app: {device}")
        store.audit("action_pause", ip=ip, device=device)
        return JSONResponse({"done": True, "control": c})

    def resume(_d, ip, _t, device):
        c = A.resume(f"app: {device}")
        store.audit("action_resume", ip=ip, device=device)
        return JSONResponse({"done": True, "control": c})

    def kill_ask(_d, ip, token, device):
        confirm = secrets.token_urlsafe(16)
        kill_asks[hashlib.sha256(token.encode()).hexdigest()] = (confirm, time.monotonic() + KILL_CONFIRM_SEC)
        store.audit("action_kill_asked", ip=ip, device=device)
        return JSONResponse({"confirm": confirm, "expires_in": KILL_CONFIRM_SEC,
                             "warning": "This cancels waiting entries, closes ALL positions and stops the bot."})

    def kill_confirm(data, ip, token, device):
        key = hashlib.sha256(token.encode()).hexdigest()
        want, until = kill_asks.pop(key, ("", 0.0))
        if not want or time.monotonic() > until or not hmac.compare_digest(str(data.get("confirm", "")), want):
            raise AuthError("that confirmation expired or does not match: ask again", 409)
        c = A.kill(f"app: {device}")
        store.audit("action_kill", ip=ip, device=device)
        store.notify(f"⛔ <b>Emergency stop from the app</b> by “{_esc(device)}”. The bot closes positions and stops "
                     "(left paused). Check /status and the Angel One app.")
        return JSONResponse({"done": True, "control": c})

    def restart(_d, ip, _t, device):
        ok, why = A.restart(f"app: {device}", runner=restart_runner)
        store.audit("action_restart", ip=ip, device=device, ok=ok, detail=why)
        if not ok:
            raise AuthError(why or "restart failed", 409)
        store.notify(f"🔄 <b>Paper bot restart from the app</b> by “{_esc(device)}”.")
        return JSONResponse({"done": True})

    def set_setting(data, ip, _t, device):
        sid = str(data.get("id", ""))
        if ("choice" in data) == ("amount" in data):
            raise BadRequest("send exactly one of choice or amount")
        if "choice" in data:
            if not isinstance(data["choice"], int) or isinstance(data["choice"], bool):
                raise BadRequest("choice must be a whole number")
            old, new = A.set_setting(sid, index=data["choice"])
        else:
            old, new = A.set_setting(sid, value=data["amount"])
        store.audit("action_setting", ip=ip, device=device, setting=sid, old=old, new=new)
        return JSONResponse({"done": True, "id": sid, "old": old, "new": new,
                             "note": "applies from the next start; not backtested"})

    def reset_settings(_d, ip, _t, device):
        n = A.reset_settings()
        store.audit("action_settings_reset", ip=ip, device=device, count=n)
        return JSONResponse({"done": True, "reset": n})

    def remove_device(data, ip, token):
        s = store.session(token)
        name = store.remove_device(str(data.get("id", "")), by=f"app: {s['name']}" if s else "app", ip=ip)
        return JSONResponse({"removed": name})

    # ---- live status
    async def ws(sock: WebSocket):
        if sock.headers.get("origin") != store.origin or not store.session(sock.cookies.get(COOKIE)):
            await sock.close(code=1008)
            return
        await sock.accept()
        last = None
        try:
            while True:
                if not store.session(sock.cookies.get(COOKIE)):          # signed out or removed meanwhile
                    await sock.close(code=1008)
                    return
                s = await asyncio.to_thread(status_payload)
                if s.get("written_at") != last:
                    last = s.get("written_at")
                    await sock.send_json({"type": "status", "status": s})
                await asyncio.sleep(WS_PUSH_SEC)
        except (WebSocketDisconnect, RuntimeError):
            return

    cred = ("id", "credential")
    routes = [Route("/healthz", health), Route("/auth/me", me),
              Route("/auth/register/options", auth_post(reg_options, allowed=("code",)), methods=["POST"]),
              Route("/auth/register/verify", auth_post(reg_verify, allowed=cred), methods=["POST"]),
              Route("/auth/login/options", auth_post(login_options), methods=["POST"]),
              Route("/auth/login/verify", auth_post(login_verify, allowed=cred), methods=["POST"]),
              Route("/auth/stepup/options", auth_post(stepup_options, need_session=True), methods=["POST"]),
              Route("/auth/stepup/verify", auth_post(stepup_verify, need_session=True, allowed=cred), methods=["POST"]),
              Route("/auth/logout", auth_post(logout), methods=["POST"]),
              Route("/auth/devices", endpoint(devices)),
              Route("/auth/devices/remove", auth_post(remove_device, need_session=True, need_stepup=True,
                                                      allowed=("id",)), methods=["POST"]),
              Route("/api/status", endpoint(status)), Route("/api/trades", endpoint(trades)),
              Route("/api/settings", endpoint(settings)), Route("/api/alerts", endpoint(alerts_)),
              Route("/api/report", endpoint(report)), Route("/api/screener", endpoint(screener)),
              *[Route(path, auth_post(act(fn), need_session=True, need_stepup=True, allowed=allowed), methods=["POST"])
                for path, fn, allowed in (
                    ("/api/control/pause", pause, ()), ("/api/control/resume", resume, ()),
                    ("/api/control/kill/ask", kill_ask, ()), ("/api/control/kill/confirm", kill_confirm, ("confirm",)),
                    ("/api/control/restart", restart, ()),
                    ("/api/settings/set", set_setting, ("id", "choice", "amount")),
                    ("/api/settings/reset", reset_settings, ()))],
              WebSocketRoute("/ws", ws)]
    return Starlette(routes=routes)


def origin_for(rp_id: str, port: int, dev: bool) -> str:
    scheme = "http" if dev else "https"
    default = 80 if dev else 443
    return f"{scheme}://{rp_id}" + ("" if port == default else f":{port}")


def main(argv=None) -> None:
    C.load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="angelbot-api", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="this VM's Tailscale address (tailscale ip -4); 127.0.0.1 with --dev")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert", help="TLS certificate (tailscale cert)")
    ap.add_argument("--key", help="TLS key (tailscale cert)")
    ap.add_argument("--dev", action="store_true", help="local testing: 127.0.0.1, plain HTTP, open http://localhost:PORT")
    ap.add_argument("--name", help="the VM's Tailscale name, e.g. instance-20261021-1543.tailnet-abc.ts.net "
                                   "(passkeys are tied to it); localhost with --dev")
    args = ap.parse_args(argv)
    check_area()
    host = args.host or ("127.0.0.1" if args.dev else "")
    check_bind(host, args.dev)
    if not args.dev and not (args.cert and args.key):
        raise StartupError("HTTPS is required: pass --cert and --key from `tailscale cert` (or use --dev locally)")
    rp_id = "localhost" if args.dev else (args.name or "")
    if not rp_id.endswith(".ts.net") and not args.dev:
        raise StartupError("--name must be this VM's Tailscale HTTPS name (ends in .ts.net): see tailscale status")
    store = AuthStore(rp_id, origin_for(rp_id, args.port, args.dev))
    store.audit("api_started", detail=f"{rp_id}:{args.port}")
    import uvicorn
    log.warning("app API (read-only) for the paper area on %s://%s:%d", "http" if args.dev else "https", host, args.port)
    uvicorn.run(create_app(store, tls=not args.dev), host=host, port=args.port, log_level="info",
                ssl_certfile=None if args.dev else args.cert, ssl_keyfile=None if args.dev else args.key,
                server_header=False, proxy_headers=False)


if __name__ == "__main__":
    main()
