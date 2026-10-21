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
mkdir -p runtime data/candles

echo "==> Unit tests"
./.venv/bin/python -m pytest -q || { echo "tests failed — fix before scheduling"; exit 1; }

echo "==> systemd user service + weekday timer (runs without you being logged in)"
sudo loginctl enable-linger "$USER"
mkdir -p ~/.config/systemd/user
cp deploy/angelbot.service deploy/angelbot.timer ~/.config/systemd/user/
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user daemon-reload
systemctl --user enable angelbot.timer
systemctl --user start angelbot.timer
systemctl --user list-timers angelbot.timer --no-pager || true

echo "==> Pin MCP server to absolute paths (works regardless of Claude Code's cwd)"
jq --arg py "$PROJECT_DIR/.venv/bin/python" --arg srv "$PROJECT_DIR/mcp_server/server.py" \
  '.mcpServers.angelbot.command=$py | .mcpServers.angelbot.args=[$srv]' \
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
  2. ./.venv/bin/python -m bot.cli check   # login test + shows this VM's public IP
     → register that IP as the static IP of your SmartAPI app (needed for orders)
  3. ./.venv/bin/python -m bot.cli fetch SBIN-EQ --days 60
     ./.venv/bin/python -m bot.cli backtest SBIN-EQ
  4. claude                            # in ~/angelbot — approve the 'angelbot' MCP server, try /status
The timer starts the bot (paper mode) at 09:05 IST every weekday.
Logs: journalctl --user -u angelbot -f     Status: ./.venv/bin/python -m bot.cli status
EOF
