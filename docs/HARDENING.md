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

## 6. Connect local Claude to the VM over SSH

Instead of running Claude Code on the VM, your laptop's Claude can use the VM's MCP server through SSH.
It gets **only the bot's tools** (status, trades, journal queries, logs, backtests, pause), not a shell,
and the `.env` file is never exposed. A forced-command key makes this a hard limit.

**On the laptop**, make a dedicated key (set a passphrase):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/angelbot_mcp -C "angelbot-mcp"
ssh-add --apple-use-keychain ~/.ssh/angelbot_mcp      # macOS: lets ssh use it without a prompt
```

**On the VM**, append the public key to `~/.ssh/authorized_keys` of the user that should run the server,
with these options in front. Use absolute paths. The options apply whatever command the client asks for:

```
command="cd /home/ubuntu/angelbot && exec ./.venv/bin/python mcp_server/server.py",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA...your-public-key... angelbot-mcp
```

- If you set up the separate Claude user (sections 2-4), put the line in `/home/claude/.ssh/authorized_keys`
  and start the server from the clone, with the shared paths in front:
  `command="ANGELBOT_RUNTIME=/home/ubuntu/angelbot/runtime ANGELBOT_DATA=/home/ubuntu/angelbot/data exec /home/claude/angelbot-dev/.venv/bin/python /home/claude/angelbot-dev/mcp_server/server.py",no-pty,...`
- Optionally add `from="<your IP>"` to the options if your IP is stable.
- `AllowUsers` in section 1 must include that user.

**On the laptop**, add to `~/.ssh/config`:

```
Host angelbot-vm
  HostName <RESERVED_IP>
  User ubuntu                       # or: claude
  IdentityFile ~/.ssh/angelbot_mcp
  IdentitiesOnly yes
  BatchMode yes
  ServerAliveInterval 30
  AddKeysToAgent yes
  UseKeychain yes                   # macOS
```

Register it with Claude Code. The name `angelbot` matches the permission rules in `.claude/settings.json`
and overrides the repo's own local server:

```bash
claude mcp add angelbot --scope local -- ssh angelbot-vm
```

**Verify:**

```bash
ssh angelbot-vm            # should sit silently (no banner, no shell prompt). Ctrl-C to quit
ssh -i ~/.ssh/angelbot_mcp ubuntu@<RESERVED_IP> ls     # must NOT list files: the forced command ignores it
```

Then in Claude run `/mcp` (expect `angelbot` connected) and `/status`.

**Limits and cautions**

- Edit and Bash in your local Claude still act on the laptop's copy of the repo. Config or code changes reach the VM
  only after you commit, push, `git pull` on the VM and restart between sessions.
- Pause, resume, kill switch and `fetch_candles` still ask for approval. Anything the tools return is sent to Anthropic.
- The private key is only as safe as your laptop. Keep the passphrase, and remove the line from `authorized_keys`
  if the laptop is lost.
- With the separate-user setup, `pause_bot`/`resume_bot` fail by design (no write access to `runtime/`). Use Telegram.

## 7. What not to do in live mode

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
