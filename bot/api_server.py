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
from datetime import datetime, timezone
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
from bot.api_push import Pusher, PushError
from bot.journal import query_readonly
from bot.timeutil import market_clock, now_ist

log = logging.getLogger("angelbot.api")

READS_PER_MIN = 120       # per device; a screen can make a few reads at once
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
APP_DIR = C.ROOT / "app"
# the web app's files: (file, content type). Served without login: they hold no data, only the screens.
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/sw.js": ("sw.js", "text/javascript; charset=utf-8"), "/icon.svg": ("icon.svg", "image/svg+xml"),
          "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png")}
# report and screener pages are shown inside the app: their own inline styles only, no scripts, framed by the app only
EMBED_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; base-uri 'none'"


# added to the report and screener pages when the app shows them (?embed=1): phone-sized type and spacing, the app's
# own colours, and no "open in your browser" hint (the Telegram copies are unchanged)
EMBED_STYLE = """<style>
:root{--bg:#fff;--panel:#f2f2f7;--line:#e5e5ea;--accent:#0a66ff}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--panel:#2c2c2e;--line:#3a3a3c;--accent:#3b8bff}}
html,body{font:13px/1.4 -apple-system,BlinkMacSystemFont,system-ui,sans-serif!important;-webkit-text-size-adjust:100%}
.wrap{padding:12px 12px 20px!important;gap:10px!important}
h1{font:700 18px/1.2 -apple-system,system-ui,sans-serif!important;margin:0!important;letter-spacing:-.01em}
h2{font-size:12px!important}.ph{font:700 16px/1.25 -apple-system,system-ui,sans-serif!important}
.sub,.note,.kpi .l,.kpi .n,.pick .rk,.chips{font-size:11.5px!important}
.card{padding:10px 12px!important;border:0!important;border-radius:10px!important}
.banner{padding:8px 10px!important;font-size:12px!important;border:0!important}
.kpis,.picks{gap:8px!important}.kpis{grid-template-columns:1fr 1fr!important}
.kpi .v{font:700 19px/1.2 -apple-system,system-ui,sans-serif!important}
.pick .sym{font:700 16px/1.2 -apple-system,system-ui,sans-serif!important}.pick .sc b{font-size:15px!important}
.chips b{font-size:13px!important}.take{font-size:13.5px!important}
table{font-size:12px!important}th,td{padding:6px 4px!important}
.tabs{margin-bottom:6px!important}.tabs label{font-size:13px!important;padding:7px 10px!important}
.bar .lab,.bar .num{font-size:12px!important}
.tabhint,footer{display:none!important}
</style>"""


def embed(page: str) -> str:
    return page.replace("</head>", EMBED_STYLE + "</head>", 1)


def app_csp(origin: str) -> str:
    ws = origin.replace("https://", "wss://").replace("http://", "ws://")
    return ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self' {ws}; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
            "object-src 'none'; manifest-src 'self'; worker-src 'self'")


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
_SESSION: dict = {}


def _session_cfg() -> dict:
    """config.yaml's session block (holidays, shutdown), re-read only when the file changes."""
    try:
        mtime = C.CONFIG_PATH.stat().st_mtime
        if _SESSION.get("mtime") != mtime:
            _SESSION.update(mtime=mtime, session=C.load_config().get("session") or {})
    except Exception as e:                                # a bad config must not break the status screen
        log.warning("session config unreadable: %s", e)
    return _SESSION.get("session") or {}


def status_payload() -> dict:
    s = control.read_status()
    s["control"] = control.read_control()
    s["market"] = market_clock(now_ist(), _session_cfg())
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


_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,30}$")


HISTORY_DAYS = 31        # charts go back this far (the bot keeps 14 trading days; the nightly candle files about 30)


def bars_payload(symbol: str | None, date: str | None = None) -> dict:
    """One day's 1-minute bars for one stock: the bot's own (runtime/<area>/bars.json today, bars/<date>.json for
    earlier days), else the nightly job's candle file (data/candles). Read-only; never asks the broker."""
    symbol = str(symbol or "")
    if not _SYMBOL.match(symbol):
        raise BadRequest("symbol must look like SBIN-EQ")
    today = now_ist().strftime("%Y-%m-%d")
    day = date or today
    if not _DATE.match(day):
        raise BadRequest("date must be YYYY-MM-DD")
    try:
        age = (datetime.fromisoformat(today) - datetime.fromisoformat(day)).days
    except ValueError:
        raise BadRequest("date must be a real day") from None
    if age < 0 or age > HISTORY_DAYS:
        raise BadRequest(f"date must be within the last {HISTORY_DAYS} days")
    for f in ((control.BARS,) if day == today else ()) + (control.BARS_DIR / f"{day}.json",):
        data = control.read_json(f)
        rows = (data.get("bars") or {}).get(symbol) if data.get("date") == day else None
        if rows:
            return {"symbol": symbol, "date": day, "timeframe_min": data.get("timeframe_min", 1), "source": "bot",
                    "bars": rows[-400:]}
    from bot import data as D
    path = D.csv_path(symbol)                               # the nightly per-stock backtests keep these
    if path.exists():
        rows = [r for r in D.load_csv(path) if str(r[0])[:10] == day]
        if rows:
            return {"symbol": symbol, "date": day, "timeframe_min": 1, "source": "candles", "bars": rows[-400:]}
    raise ValueError(f"no bars for {symbol} on {day}")


def settings_payload() -> dict:
    from bot import settings_guide as SG
    cfg = ST.load()
    vals = ST.overrides()
    guide = SG.payload(cfg, vals, ST.problems(), os.environ.get("ANGELBOT_PROFILE"))
    items = []
    for s in ST.SETTINGS:
        cur = ST.current(cfg, s)
        items.append({"id": s["id"], "label": s["label"], "value": ST.show(s, cur), "changed": s["path"] in vals,
                      "choices": [{"index": i, "label": ST.show(s, c), "current": c == cur} for i, c in enumerate(s["choices"])],
                      "typed": bool(s.get("typed"))})
    return {**guide, "settings": items}                    # "settings": the flat list (kept for older app screens)


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


def create_app(store: AuthStore, tls: bool = True, restart_runner=subprocess.run, pusher: Pusher | None = None) -> Starlette:
    """`store` holds devices and sessions (bot.api_auth). Reads need a session; actions need a session, the right
    Origin, the X-Requested-With header (CSRF) and a fresh step-up (a passkey check from the last 2 minutes)."""
    pusher = pusher or Pusher(store)
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
        for k, v in SECURITY_HEADERS.items():          # a route may set its own policy (the app, embedded pages)
            if k not in resp.headers:
                resp.headers[k] = v
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

    def bars(r):
        return JSONResponse(bars_payload(r.query_params.get("symbol"), r.query_params.get("date")))

    def alerts_(r):
        limit = r.query_params.get("limit", "50")
        if not limit.isdigit():
            raise BadRequest("limit must be a number")
        return JSONResponse({"alerts": alerts.read(r.query_params.get("since"), int(limit))})

    def report(r):
        page, _caption = _report_html(" ".join(x for x in (r.query_params.get("source"), r.query_params.get("since")) if x))
        if r.query_params.get("embed") == "1":
            page = embed(page)
        return HTMLResponse(page, headers={"Content-Security-Policy": EMBED_CSP, "X-Frame-Options": "SAMEORIGIN"})

    def screener(r):
        page, _summary = _screener_html()
        if r.query_params.get("embed") == "1":
            page = embed(page)
        return HTMLResponse(page, headers={"Content-Security-Policy": EMBED_CSP, "X-Frame-Options": "SAMEORIGIN"})

    csp = app_csp(store.origin)

    async def static(request: Request):
        name, ctype = STATIC[request.url.path]
        try:
            body = (APP_DIR / name).read_bytes()
        except FileNotFoundError:
            return err("not found", 404)
        return secure(Response(body, media_type=ctype, headers={"Content-Security-Policy": csp,
                                                                 "Cache-Control": "no-cache"}))

    def devices(_r):
        return JSONResponse({"devices": store.device_list()})

    async def health(_r):
        return secure(JSONResponse({"ok": True}))        # no details: says only that the service answers

    async def me(request: Request):
        s = store.session(request.cookies.get(COOKIE))
        if not s:
            return secure(JSONResponse({"signed_in": False, "registered": bool(store.devices())}))
        return secure(JSONResponse({"signed_in": True, "device": s["name"], "expires": s["expires"],
                                    "stepped_up": store.stepped_up(request.cookies.get(COOKIE)),
                                    "push": pusher.device_has(s["device"])}))

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

    # ---- push notifications (phase 6): a signed-in device turns them on or off for itself; no step-up needed
    def push_key(_r):
        return JSONResponse({"key": pusher.public_key()})

    def push_subscribe(data, ip, token):
        s = store.session(token)
        try:
            pusher.subscribe(data.get("subscription"), s["device"], s["name"])
        except PushError as e:
            raise BadRequest(str(e)) from None
        return JSONResponse({"subscribed": True})

    def push_unsubscribe(data, ip, token):
        s = store.session(token)
        return JSONResponse({"unsubscribed": pusher.unsubscribe(str(data.get("endpoint", "")), s["device"])})

    def push_test(_d, ip, token):
        s = store.session(token)
        r = pusher.send("📝 PAPER · Test notification", f"Notifications work on {s['name']}.", only_device=s["device"])
        return JSONResponse(r)

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
        gone = asyncio.Event()

        async def watch():                     # the phone left (or the server is stopping): end the loop at once
            try:
                while True:
                    if (await sock.receive()).get("type") == "websocket.disconnect":
                        break
            except Exception:
                pass
            gone.set()

        watcher = asyncio.create_task(watch())
        last = None
        try:
            while not gone.is_set():
                if not store.session(sock.cookies.get(COOKIE)):          # signed out or removed meanwhile
                    await sock.close(code=1008)
                    return
                s = await asyncio.to_thread(status_payload)
                if s.get("written_at") != last:
                    last = s.get("written_at")
                    await sock.send_json({"type": "status", "status": s})
                try:
                    await asyncio.wait_for(gone.wait(), WS_PUSH_SEC)
                except asyncio.TimeoutError:
                    pass
        except (WebSocketDisconnect, RuntimeError, OSError):
            return
        finally:
            watcher.cancel()

    cred = ("id", "credential")
    routes = [*[Route(path, static) for path in STATIC],
              Route("/healthz", health), Route("/auth/me", me),
              Route("/auth/register/options", auth_post(reg_options, allowed=("code",)), methods=["POST"]),
              Route("/auth/register/verify", auth_post(reg_verify, allowed=cred), methods=["POST"]),
              Route("/auth/login/options", auth_post(login_options), methods=["POST"]),
              Route("/auth/login/verify", auth_post(login_verify, allowed=cred), methods=["POST"]),
              Route("/auth/stepup/options", auth_post(stepup_options, need_session=True), methods=["POST"]),
              Route("/auth/stepup/verify", auth_post(stepup_verify, need_session=True, allowed=cred), methods=["POST"]),
              Route("/auth/logout", auth_post(logout), methods=["POST"]),
              Route("/auth/devices", endpoint(devices)),
              Route("/api/push/key", endpoint(push_key)),
              Route("/api/push/subscribe", auth_post(push_subscribe, need_session=True, allowed=("subscription",)), methods=["POST"]),
              Route("/api/push/unsubscribe", auth_post(push_unsubscribe, need_session=True, allowed=("endpoint",)), methods=["POST"]),
              Route("/api/push/test", auth_post(push_test, need_session=True), methods=["POST"]),
              Route("/auth/devices/remove", auth_post(remove_device, need_session=True, need_stepup=True,
                                                      allowed=("id",)), methods=["POST"]),
              Route("/api/status", endpoint(status)), Route("/api/trades", endpoint(trades)),
              Route("/api/settings", endpoint(settings)), Route("/api/alerts", endpoint(alerts_)),
              Route("/api/bars", endpoint(bars)),
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


CERT_WARN_DAYS = 14


def check_cert(cert: str, key: str, name: str, now: datetime | None = None) -> int:
    """The certificate must exist, cover `name` and not have expired; returns days left (alerting under 14)."""
    from cryptography import x509
    for path in (cert, key):
        if not Path(path).is_file():
            raise StartupError(f"missing {path}: run deploy/app_cert.sh paper (it uses tailscale cert)")
    try:
        c = x509.load_pem_x509_certificate(Path(cert).read_bytes())
    except ValueError:
        raise StartupError(f"{cert} is not a PEM certificate") from None
    try:
        names = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    if name not in names:
        raise StartupError(f"the certificate is for {', '.join(names) or 'no name'}, not {name}")
    now = now or datetime.now(timezone.utc)
    left = (c.not_valid_after_utc - now).days
    if c.not_valid_after_utc <= now:
        raise StartupError(f"the certificate expired on {c.not_valid_after_utc:%Y-%m-%d}: run deploy/app_cert.sh paper")
    if left < CERT_WARN_DAYS:
        from bot import notify
        notify.send(f"⚠️ <b>App certificate expires in {left} days</b>. Renewal should run daily: check "
                    "<code>systemctl --user status angelbot-tlscert@paper.timer</code>")
    return left


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
    if not args.dev:
        log.info("certificate valid for %d more days", check_cert(args.cert, args.key, rp_id))
    store = AuthStore(rp_id, origin_for(rp_id, args.port, args.dev))
    store.audit("api_started", detail=f"{rp_id}:{args.port}")
    pusher = Pusher(store)
    pusher.start()                                    # bot alerts -> push notifications
    import uvicorn
    log.warning("app API for the paper area on %s://%s:%d", "http" if args.dev else "https", rp_id, args.port)
    uvicorn.run(create_app(store, tls=not args.dev, pusher=pusher), host=host, port=args.port, log_level="info",
                ssl_certfile=None if args.dev else args.cert, ssl_keyfile=None if args.dev else args.key,
                server_header=False, proxy_headers=False)


if __name__ == "__main__":
    main()
