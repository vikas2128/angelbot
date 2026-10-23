"""Phase 5: the web app's files. Served without login (they hold no data), under a strict policy: scripts and styles
only from this server, nothing inline, nothing from outside."""
import json
import re
import shutil
import struct
import subprocess

import pytest
from starlette.testclient import TestClient

from bot import api_auth as AA
from bot import api_server as API
from bot import control

RP = "bot.tail1234.ts.net"
ORIGIN = f"https://{RP}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    store = AA.AuthStore(RP, ORIGIN, folder=tmp_path / "app", notify_fn=lambda t: None)
    with TestClient(API.create_app(store, tls=True), base_url=ORIGIN) as c:
        yield c


def test_every_app_file_is_served_with_the_strict_policy(client):
    for path, (name, ctype) in API.STATIC.items():
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].split(";")[0] == ctype.split(";")[0], path
        csp = r.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "style-src 'self'" in csp and "unsafe" not in csp, path
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and f"wss://{RP}" in csp
        assert r.headers["Cache-Control"] == "no-cache" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/app.js.map").status_code == 404 and client.get("/../config/config.yaml").status_code == 404


def test_no_inline_code_no_outside_resources_no_html_injection():
    html = (API.APP_DIR / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html)                  # every script is a file
    assert not re.search(r"\sstyle=|\son[a-z]+=", html)                          # no inline styles or handlers
    js = re.sub(r"^\s*//.*$", "", (API.APP_DIR / "app.js").read_text(), flags=re.M)     # code only, not comments
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "style:"):
        assert bad not in js, bad
    for name in ("index.html", "app.js", "app.css", "sw.js", "manifest.webmanifest"):
        urls = set(re.findall(r"https?://[^\s'\"`)]+", (API.APP_DIR / name).read_text()))
        assert not urls, (name, urls)                                            # nothing loaded from outside


def test_manifest_and_icons_are_installable():
    m = json.loads((API.APP_DIR / "manifest.webmanifest").read_text())
    assert m["display"] == "standalone" and m["start_url"] == "/" and m["scope"] == "/"
    for icon in m["icons"]:
        f = API.APP_DIR / icon["src"].lstrip("/")
        assert f.exists(), icon
        if icon["type"] == "image/png":
            w, h = struct.unpack(">II", f.read_bytes()[16:24])
            assert f"{w}x{h}" == icon["sizes"], icon
    assert any(i["sizes"] == "512x512" for i in m["icons"]) and any(i["sizes"] == "192x192" for i in m["icons"])


def test_service_worker_never_caches_data():
    sw = (API.APP_DIR / "sw.js").read_text()
    assert r"/^\/(api|auth|ws|healthz)\b/" in sw and "method !== 'GET'" in sw


def test_report_and_screener_can_be_framed_by_the_app_only(client, tmp_path):
    from test_screener import CHARGES, NOW, raw
    from bot.screener import params_from, rank
    control.write_screen(rank([raw("SBIN-EQ")], params_from(None), CHARGES, NOW))
    store = AA.AuthStore(RP, ORIGIN, folder=tmp_path / "app", notify_fn=lambda t: None)
    from test_api import _signed_in
    _signed_in(store, client)
    r = client.get("/api/screener")
    assert r.status_code == 200 and r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["Content-Security-Policy"].startswith("default-src 'none'; style-src 'unsafe-inline'")
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"] and "script-src" not in r.headers["Content-Security-Policy"]
    assert client.get("/api/status").headers["X-Frame-Options"] == "DENY"        # data stays unframeable


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_scripts_parse():
    for name in ("app.js", "sw.js"):
        subprocess.run(["node", "--check", str(API.APP_DIR / name)], check=True)
