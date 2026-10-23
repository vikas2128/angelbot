#!/usr/bin/env bash
# Fetch or renew the app's HTTPS certificate from Tailscale into runtime/<area>/app/tls/, and restart the API if it
# changed. Needs `sudo tailscale set --operator=$USER` once (deploy/app_setup.sh does it). Usage: deploy/app_cert.sh paper
set -euo pipefail
cd "$(dirname "$0")/.."
area="${1:-paper}"
[[ "$area" == "paper" ]] || { echo "the app is only for the paper area" >&2; exit 2; }
env_file="runtime/$area/app/api.env"
[[ -f "$env_file" ]] || { echo "missing $env_file: run deploy/app_setup.sh first" >&2; exit 2; }
# shellcheck disable=SC1090
source "$env_file"
tls="runtime/$area/app/tls"
mkdir -p "$tls" && chmod 700 "$tls"
before="$(sha256sum "$tls/cert.pem" 2>/dev/null || true)"
if ! tailscale cert --cert-file "$tls/cert.pem.new" --key-file "$tls/key.pem.new" "$ANGELBOT_API_NAME"; then
  msg="App certificate renewal FAILED for $ANGELBOT_API_NAME. Check: journalctl --user -u angelbot-tlscert@$area"
  echo "$msg" >&2
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    curl -fsS -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=⚠️ $msg" >/dev/null || true
  fi
  exit 1
fi
chmod 600 "$tls/cert.pem.new" "$tls/key.pem.new"
mv -f "$tls/cert.pem.new" "$tls/cert.pem"
mv -f "$tls/key.pem.new" "$tls/key.pem"
after="$(sha256sum "$tls/cert.pem")"
if [[ "$before" != "$after" ]]; then
  echo "certificate updated for $ANGELBOT_API_NAME"
  systemctl --user try-restart "angelbot-api@$area.service" || true
else
  echo "certificate unchanged"
fi
