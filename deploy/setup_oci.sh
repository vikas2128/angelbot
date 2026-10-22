#!/usr/bin/env bash
# One-shot setup for an OCI Ubuntu 22.04/24.04 VM (Ampere A1 ARM or AMD micro).
# Run as the default 'ubuntu' user from inside the project:   cd ~/angelbot && bash deploy/setup_oci.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ "$PROJECT_DIR" != "$HOME/angelbot" ]]; then
  echo "⚠  Project is at $PROJECT_DIR; the systemd unit expects ~/angelbot. Move it there or edit deploy/angelbot.service."
  exit 1
fi

echo "==> System packages, timezone (IST) and clock sync"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip git curl chrony sqlite3 jq
sudo timedatectl set-timezone Asia/Kolkata
sudo systemctl enable --now chrony

echo "==> Swap (helps on 1 GB AMD micro shapes)"
if [[ $(free -m | awk '/Mem:/ {print $2}') -lt 2048 ]] && ! swapon --show | grep -q swapfile; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> Python venv + dependencies"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip wheel -q
./.venv/bin/pip install -r requirements.txt -q

echo "==> Secrets file"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "   created .env — edit it now:  nano .env"
fi
chmod 600 .env
mkdir -p runtime/paper runtime/live runtime/research data/candles

echo "==> Area files (paper / research; live is prepared but left off)"
for area in paper research; do
  if [[ ! -f "profiles/$area.env" ]]; then
    cp "profiles/$area.env.example" "profiles/$area.env"
    echo "   created profiles/$area.env: set its Telegram bot token and chat id"
  fi
  chmod 600 "profiles/$area.env"
done

echo "==> Unit tests"
./.venv/bin/python -m pytest -q || { echo "tests failed — fix before scheduling"; exit 1; }

echo "==> systemd user service + weekday timer (runs without you being logged in)"
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp deploy/angelbot@.service deploy/angelbot@.timer deploy/angelbot-telegram@.service ~/.config/systemd/user/
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user daemon-reload
# Only the PAPER area is switched on. Live is installed as a template but is never enabled or started here:
# going live is a manual step (see profiles/live.env.example and docs/ENVIRONMENTS.md).
systemctl --user enable angelbot@paper.timer
systemctl --user start angelbot@paper.timer
systemctl --user enable angelbot-telegram@paper.service
systemctl --user restart angelbot-telegram@paper.service || true   # exits quietly until the paper bot token is set
systemctl --user list-timers 'angelbot@*' --no-pager || true

echo "==> Pin MCP server to absolute paths (works regardless of Claude Code's cwd)"
jq --arg py "$PROJECT_DIR/.venv/bin/python" --arg srv "$PROJECT_DIR/mcp_server/server.py" \
  '.mcpServers.angelbot.command=$py | .mcpServers.angelbot.args=[$srv] | .mcpServers.angelbot.env.ANGELBOT_PROFILE="paper"' \
  .mcp.json > .mcp.json.tmp && mv .mcp.json.tmp .mcp.json

echo "==> Claude Code CLI"
if ! command -v claude >/dev/null 2>&1; then
  curl -fsSL https://claude.ai/install.sh | bash
  grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
fi

cat <<'EOF'

✅ Setup complete.

Next:
  1. nano .env                         # fill SmartAPI key, client code, PIN, TOTP secret (mode stays paper)
     nano profiles/paper.env           # the PAPER Telegram bot token + your chat id
     nano profiles/research.env        # the TEST Telegram bot token (replays send one summary there)
  2. ./deploy/ab paper check           # login test + shows this VM's public IP
     → register that IP as the static IP of your SmartAPI app (needed for orders)
  3. ./deploy/ab paper fetch SBIN-EQ --days 60
     ./deploy/ab research backtest SBIN-EQ
  4. claude                            # in ~/angelbot — approve the 'angelbot' MCP server, try /status
The timer starts the PAPER bot at 09:05 IST every weekday. Live is prepared but off (docs/ENVIRONMENTS.md).
Logs: journalctl --user -u angelbot@paper -f     Status: ./deploy/ab paper status
EOF
