# VM hardening: running Claude Code next to AngelBot

The defaults in `.claude/settings.json` (deny `.env`, no order tool, read-only journal) are
**permission rules, not walls**. If Claude Code runs as the same Linux user as the bot, that user can
read `.env` (broker credentials + paper/live switch) and edit the bot's code. This guide makes the
rules real by giving Claude its own user. Do it **before going live**. Paper mode with the
default setup is fine for the learning phase.

Examples assume the bot runs as `ubuntu` in `/home/ubuntu/angelbot`. Adjust names to match. Keep a
second SSH session open while changing anything below, and **test every step** (section 5).

## 1. SSH

Create `/etc/ssh/sshd_config.d/99-hardening.conf`:

```
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
MaxAuthTries 3
AllowUsers ubuntu claude
```

```bash
sudo sshd -t && sudo systemctl reload ssh      # check the config, then apply
```

- OCI security list: ingress **TCP 22 only, from your own IP (`/32`)**. The bot needs no inbound ports.
- Public keys only, one key per person/device. Remove keys you no longer use.
- Keep the VM patched: `sudo apt install unattended-upgrades`, and reboot after kernel updates (outside market hours).

## 2. A separate Linux user for Claude

```bash
sudo adduser --disabled-password --gecos "" claude
sudo mkdir -p /home/claude/.ssh && sudo cp ~/.ssh/authorized_keys /home/claude/.ssh/   # or a separate key
sudo chown -R claude:claude /home/claude/.ssh && sudo chmod 700 /home/claude/.ssh
sudo groupadd angelbot-ops && sudo usermod -aG angelbot-ops claude
```

- **No sudo for `claude`.** Check with `sudo -l -U claude` (expect "not allowed").
- Claude gets **read-only** access to the bot's folder and works on code in **its own clone**.

## 3. Lock down `.env` and the bot's folder

```bash
chmod o+x /home/ubuntu                              # let other users walk through to ~/angelbot
chgrp -R angelbot-ops ~/angelbot
chmod -R g+rX,o-rwx ~/angelbot
chgrp ubuntu ~/angelbot/.env && chmod 600 ~/angelbot/.env     # owner only: Claude's user cannot read it
```

- Do not add `claude` to the `ubuntu` group, and do not make `.env` group-readable.
- `runtime/` stays **read-only** for Claude. `pause`/`resume`/`kill` go through Telegram, which runs as the bot's user.
  (A file written by another user can become unreadable to the bot, so do not give Claude write access to `runtime/`.)

## 4. Claude's own working copy

As `claude`:

```bash
git clone <your repo URL> ~/angelbot-dev && cd ~/angelbot-dev
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# do NOT run deploy/setup_oci.sh here: it installs systemd units and prompts for .env
echo 'export ANGELBOT_RUNTIME=/home/ubuntu/angelbot/runtime' >> ~/.bashrc    # read live status/journal/log
echo 'export ANGELBOT_DATA=/home/ubuntu/angelbot/data'       >> ~/.bashrc    # read candle CSVs for backtests
```

Then run `claude` from `~/angelbot-dev` and approve the `angelbot` MCP server.

- Claude proposes code/config changes as commits in its clone. **You** review them, `git pull` as `ubuntu`, and restart the service between sessions.
- Do not give Claude a push credential to your main account. Use a repo-scoped deploy key, or push for it yourself.
- Expect these to be unavailable for `claude`, by design: `fetch_candles` (needs the broker credentials; run `bot.cli fetch` as `ubuntu`), `systemctl --user` / `journalctl --user` for the bot's service (use `bot_status` and `tail_log` instead).

## 5. Verify (as `claude`, expect these results)

```bash
cat /home/ubuntu/angelbot/.env                          # Permission denied
touch /home/ubuntu/angelbot/bot/x                       # Permission denied
sqlite3 -readonly /home/ubuntu/angelbot/runtime/journal.db 'select count(*) from trades'   # works
sudo -n true                                            # a password is required / not allowed
```

Then in Claude, run `/status`. It should read the live status file and journal.

## 6. What not to do in live mode

- **Never run Claude Code with auto-accept / bypass-permissions on the VM.** Approvals are the last gate.
- **Never let Claude read, edit or move `.env`.** Switch paper to live **yourself**, by editing `.env` by hand and restarting. Keep the `ANGELBOT_LIVE_CONFIRM` line commented out until go-live.
- Do not add broad allow rules such as `Bash(*)`, `Bash(python:*)` or `Bash(sudo:*)` to Claude's settings.
- Do not change config or code while a session is running. Changes apply from the next session.
- Do not paste secrets (API key, PIN, TOTP secret, Telegram token) into chat, Telegram or the repo.
- Do not open extra ports or add dashboards. SSH only.
- Do not go live before you have tested `/kill CONFIRM` from Telegram in paper mode and seen the bot flatten.
- Keep only money you can afford to lose in the trading account, and keep `risk.max_daily_loss` small.
- Alerts are tagged `🔴 LIVE` or `🧪 TEST`. If you expect live and see TEST (or the reverse), stop and check `.env`.
- Anything Claude reads (logs, journal, config) is sent to Anthropic. Make sure logs never contain secrets.
