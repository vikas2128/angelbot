"""The app's API (phase 2: READ-ONLY). Serves status, trades, report, screener, settings and alerts for the PAPER area,
plus a WebSocket with live status. See docs/APP_PLAN.md.

Safety, by construction:
  * GET only: there is no route that changes anything, and none that places, modifies or cancels orders, switches
    paper/live, or reads .env, profiles or broker credentials. The API never logs in to the broker.
  * Area-locked: it refuses to run for any area but paper, and reads only that area's runtime folder.
  * Network-locked: it binds only to a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48) with HTTPS, or, with
    --dev, to 127.0.0.1 for local testing. It never binds to 0.0.0.0.
  * Interim login (until phase 3 adds passkeys): every request needs `Authorization: Bearer <token>`, a random token
    kept in runtime/paper/app/api_token (chmod 600). The WebSocket takes it as its first message.

    ./deploy/ab paper api --host 100.101.102.103 --cert <crt> --key <key>     (on the VM)
    ./deploy/ab paper api --dev                                               (local test: 127.0.0.1, plain HTTP)
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import logging
import os
import re
import secrets
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from bot import alerts, control
from bot import config as C
from bot import settings as ST
from bot.journal import query_readonly
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.api")

READS_PER_MIN = 30
WS_PUSH_SEC = 2.0
WS_AUTH_SEC = 5.0
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


def token_path() -> Path:
    return C.RUNTIME_DIR / "app" / "api_token"


def load_or_create_token() -> str:
    p = token_path()
    if p.exists():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tok = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp_")
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    log.warning("created a new API token in %s", p)
    return tok


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
def create_app(token: str, tls: bool = True) -> Starlette:
    hits: dict[str, deque] = defaultdict(deque)

    def guard(request: Request) -> Response | None:
        auth = request.headers.get("authorization", "")
        given = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not given or not hmac.compare_digest(given, token):
            log.warning("refused request without a valid token from %s", request.client.host if request.client else "?")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        who = request.client.host if request.client else "?"
        q, now = hits[who], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= READS_PER_MIN:
            return JSONResponse({"error": "too many requests, try again in a minute"}, status_code=429)
        q.append(now)
        return None

    def secure(resp: Response) -> Response:
        resp.headers.update(SECURITY_HEADERS)
        if tls:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def endpoint(fn):
        async def handler(request: Request) -> Response:
            refused = guard(request)
            if refused is not None:
                return secure(refused)
            try:
                return secure(await asyncio.to_thread(fn, request))
            except ValueError as e:                    # bad input, or nothing to show yet: safe to tell the user
                return secure(JSONResponse({"error": str(e)}, status_code=400 if isinstance(e, BadRequest) else 404))
            except Exception:
                log.exception("api error on %s", request.url.path)
                return secure(JSONResponse({"error": "internal error"}, status_code=500))
        return handler

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

    async def health(_r):
        return secure(JSONResponse({"ok": True}))        # no details: says only that the service answers

    async def ws(sock: WebSocket):
        origin = sock.headers.get("origin")
        host = sock.headers.get("host", "")
        if origin and origin.split("://", 1)[-1] != host:
            await sock.close(code=1008)
            return
        await sock.accept()
        try:
            first = await asyncio.wait_for(sock.receive_json(), WS_AUTH_SEC)
        except Exception:
            await sock.close(code=1008)
            return
        if not isinstance(first, dict) or not hmac.compare_digest(str(first.get("auth", "")), token):
            await sock.close(code=1008)
            return
        last = None
        try:
            while True:
                s = await asyncio.to_thread(status_payload)
                if s.get("written_at") != last:
                    last = s.get("written_at")
                    await sock.send_json({"type": "status", "status": s})
                await asyncio.sleep(WS_PUSH_SEC)
        except (WebSocketDisconnect, RuntimeError):
            return

    routes = [Route("/healthz", health),
              Route("/api/status", endpoint(status)), Route("/api/trades", endpoint(trades)),
              Route("/api/settings", endpoint(settings)), Route("/api/alerts", endpoint(alerts_)),
              Route("/api/report", endpoint(report)), Route("/api/screener", endpoint(screener)),
              WebSocketRoute("/ws", ws)]
    return Starlette(routes=routes)


def main(argv=None) -> None:
    C.load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="angelbot-api", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="this VM's Tailscale address (tailscale ip -4); 127.0.0.1 with --dev")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert", help="TLS certificate (tailscale cert)")
    ap.add_argument("--key", help="TLS key (tailscale cert)")
    ap.add_argument("--dev", action="store_true", help="local testing: 127.0.0.1, plain HTTP")
    args = ap.parse_args(argv)
    check_area()
    host = args.host or ("127.0.0.1" if args.dev else "")
    check_bind(host, args.dev)
    if not args.dev and not (args.cert and args.key):
        raise StartupError("HTTPS is required: pass --cert and --key from `tailscale cert` (or use --dev locally)")
    token = load_or_create_token()
    import uvicorn
    log.warning("app API (read-only) for the paper area on %s://%s:%d", "http" if args.dev else "https", host, args.port)
    uvicorn.run(create_app(token, tls=not args.dev), host=host, port=args.port, log_level="info",
                ssl_certfile=None if args.dev else args.cert, ssl_keyfile=None if args.dev else args.key,
                server_header=False, proxy_headers=False)


if __name__ == "__main__":
    main()
