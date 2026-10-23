#!/usr/bin/env bash
# One-time setup of the AngelBot app on this VM (paper area only). Safe to run again. See docs/APP.md.
#   bash deploy/app_setup.sh
# Before: Tailscale installed and logged in on this VM, MagicDNS and HTTPS certificates turned on in the admin console.
set -euo pipefail
cd "$(dirname "$0")/.."
area=paper
port="${ANGELBOT_API_PORT:-8443}"
say() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f "profiles/$area.env" ]] || die "profiles/$area.env is missing (see docs/ENVIRONMENTS.md)"
command -v tailscale >/dev/null || die "Tailscale is not installed: curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"

say "Tailscale"
json="$(tailscale status --json 2>/dev/null)" || die "Tailscale is not running or not logged in: sudo tailscale up"
read -r name ip < <(python3 -c '
import json, sys
s = json.load(sys.stdin)["Self"]
name = (s.get("DNSName") or "").rstrip(".")
ip = next((a for a in s.get("TailscaleIPs", []) if "." in a), "")
print(name or "-", ip or "-")' <<<"$json")
[[ "$name" == *.ts.net ]] || die "no MagicDNS name for this VM: turn on MagicDNS in the Tailscale admin console (DNS page)"
[[ "$ip" == 100.* ]] || die "no Tailscale IPv4 address for this VM"
echo "   name $name, address $ip, port $port"

say "Letting your user fetch HTTPS certificates (needs sudo once)"
sudo tailscale set --operator="$USER"

say "Python packages"
./.venv/bin/pip install -q -r requirements.txt

say "App settings: runtime/$area/app/api.env"
mkdir -p "runtime/$area/app" && chmod 700 "runtime/$area/app"
umask 077
cat > "runtime/$area/app/api.env" <<CONF
# written by deploy/app_setup.sh (not secret): where the app API listens and its Tailscale HTTPS name
ANGELBOT_API_HOST=$ip
ANGELBOT_API_NAME=$name
ANGELBOT_API_PORT=$port
CONF

say "HTTPS certificate"
bash deploy/app_cert.sh "$area" || die "tailscale cert failed: turn on HTTPS certificates in the Tailscale admin console (DNS page), then run this again"

say "systemd units"
mkdir -p ~/.config/systemd/user
cp deploy/angelbot-api@.service deploy/angelbot-tlscert@.service deploy/angelbot-tlscert@.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now "angelbot-tlscert@$area.timer"
systemctl --user enable "angelbot-api@$area.service"
systemctl --user restart "angelbot-api@$area.service"
sleep 4
if ! systemctl --user is-active --quiet "angelbot-api@$area.service"; then
  if journalctl --user -u "angelbot-api@$area" -n 30 --no-pager | grep -qiE "namespace|Failed to set up mount|status=226"; then
    say "This system does not allow systemd's sandbox in user services (common on Ubuntu 24.04)."
    say "Falling back to: no new privileges and resource limits only. The API's own checks still apply."
    mkdir -p ~/.config/systemd/user/"angelbot-api@$area.service.d"
    cat > ~/.config/systemd/user/"angelbot-api@$area.service.d/no-sandbox.conf" <<'DROP'
# written by deploy/app_setup.sh: this machine cannot use mount namespaces in user services
[Service]
PrivateTmp=no
ProtectSystem=no
ProtectHome=no
ReadWritePaths=
RestrictNamespaces=no
DROP
    systemctl --user daemon-reload
    systemctl --user restart "angelbot-api@$area.service"
    sleep 4
  fi
fi
systemctl --user is-active --quiet "angelbot-api@$area.service" || {
  journalctl --user -u "angelbot-api@$area" -n 40 --no-pager >&2
  die "the app API did not start (log above)"
}

cat <<DONE

The app is running at:  https://$name:$port
Only devices on your Tailscale network can reach it.

Next, register your phone:
  1. ./deploy/ab $area app enroll --name "My phone"      (prints a code valid for 10 minutes)
  2. On the phone, with Tailscale on, open https://$name:$port
  3. Type the code, confirm with Face ID or fingerprint, then Add to Home Screen.
  4. Settings tab -> Turn on notifications.
DONE
