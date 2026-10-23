"""Phase 7: deploying the app on the VM. The unit's hardening, the scripts' refusals and the certificate checks."""
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from bot import api_server as API

ROOT = Path(__file__).resolve().parent.parent
NAME = "instance-20261021-1543.tail1234.ts.net"


def unit(name: str) -> str:
    return (ROOT / "deploy" / name).read_text()


def test_api_unit_is_hardened_and_area_scoped():
    u = unit("angelbot-api@.service")
    for line in ("NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=read-only",
                 "ReadWritePaths=%h/angelbot/runtime/%i %t", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
                 "UMask=0077", "MemoryMax=400M", "CPUQuota=50%", "Restart=on-failure"):
        assert line in u, line
    assert "EnvironmentFile=%h/angelbot/runtime/%i/app/api.env" in u
    assert "--host ${ANGELBOT_API_HOST}" in u and "--name ${ANGELBOT_API_NAME}" in u
    assert "--cert %h/angelbot/runtime/%i/app/tls/cert.pem" in u and "--key %h/angelbot/runtime/%i/app/tls/key.pem" in u
    assert "0.0.0.0" not in u and "--dev" not in u


def test_cert_renewal_runs_daily_and_restarts_the_api_only_on_change():
    t, s = unit("angelbot-tlscert@.timer"), unit("app_cert.sh")
    assert "OnCalendar=*-*-* 06:10:00 Asia/Kolkata" in t and "Persistent=true" in t
    assert "tailscale cert --cert-file" in s and 'try-restart "angelbot-api@$area.service"' in s
    assert 'chmod 600 "$tls/cert.pem.new" "$tls/key.pem.new"' in s and "chmod 700" in s


@pytest.mark.parametrize("script", ["app_setup.sh", "app_cert.sh"])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / script)], check=True)


def test_scripts_never_touch_live():
    assert subprocess.run(["bash", str(ROOT / "deploy" / "app_cert.sh"), "live"], capture_output=True).returncode == 2
    setup = unit("app_setup.sh")
    assert "area=paper" in setup and "@live" not in setup and "angelbot@" not in setup.replace("angelbot-api@", "")


def _cert(tmp_path, name=NAME, days=60, expired=False):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    start, end = (now - timedelta(days=100), now - timedelta(days=1)) if expired else (now - timedelta(days=1), now + timedelta(days=days))
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
         .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False).sign(key, hashes.SHA256()))
    cp, kp = tmp_path / "cert.pem", tmp_path / "key.pem"
    cp.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cp), str(kp)


def test_certificate_checks(tmp_path, monkeypatch):
    sent = []
    from bot import notify
    monkeypatch.setattr(notify, "send", lambda t, b=None: sent.append(t))
    cert, key = _cert(tmp_path)
    assert 58 <= API.check_cert(cert, key, NAME) <= 60 and sent == []
    with pytest.raises(SystemExit, match="not other.tail1234.ts.net"):
        API.check_cert(cert, key, "other.tail1234.ts.net")
    with pytest.raises(SystemExit, match="missing"):
        API.check_cert(str(tmp_path / "nope.pem"), key, NAME)
    soon = tmp_path / "soon"; soon.mkdir()
    API.check_cert(*_cert(soon, days=5), NAME)
    assert any("expires in" in m for m in sent)                          # warned on Telegram
    old = tmp_path / "old"; old.mkdir()
    with pytest.raises(SystemExit, match="expired"):
        API.check_cert(*_cert(old, expired=True), NAME)
    bad = tmp_path / "bad.pem"; bad.write_text("not a cert")
    with pytest.raises(SystemExit, match="not a PEM"):
        API.check_cert(str(bad), key, NAME)


def test_main_checks_the_certificate_before_listening(tmp_path, monkeypatch):
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    with pytest.raises(SystemExit, match="missing"):
        API.main(["--host", "100.101.102.103", "--name", NAME, "--cert", str(tmp_path / "c.pem"), "--key", str(tmp_path / "k.pem")])
