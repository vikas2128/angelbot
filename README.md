# AngelBot — Angel One SmartAPI intraday bot on OCI, operated with Claude Code

```
            Oracle Cloud VM (Mumbai/Hyderabad, reserved static IP)
 ┌──────────────────────────────────────────────────────────────────────┐
 │  systemd timer 09:05 IST Mon–Fri ─▶ angelbot.service (python -m bot.cli run)
 │                                                                      │
 │  SmartAPI WebSocket ─ticks─▶ BarBuilder ─▶ Strategy ─▶ RiskManager    │
 │                                   │                       │          │
 │        stops/targets on every tick│              OrderManager (LIMIT) │──▶ SmartAPI REST
 │                                   ▼                       ▼          │    (live) or
 │      runtime/status.json   runtime/journal.db   runtime/bot.log       │    PaperBroker
 │              ▲   control.json (pause/kill)                           │
 │              │                                                       │
 │  Claude Code CLI ──stdio──▶ mcp_server (angelbot) : status, trades,   │
 │   (you, over SSH)            journal SQL, logs, backtests, sweeps,    │
 │                              pause / resume / kill-switch             │
 └──────────────────────────────────────────────────────────────────────┘
```

**Who decides what**
- The **coded strategy + risk rules** make every real-time trading decision (milliseconds, deterministic).
- **Claude Code** is your analyst/operator: health checks, end-of-day reviews, backtests,
  walk-forward tuning, writing new strategies, debugging. It **cannot place orders** and
  **cannot switch to live** — by design.

**Safety defaults:** paper mode · LIMIT orders only · ≤ 3 orders/sec · stop-loss on every
trade · daily loss limit → flatten + halt · consecutive-loss cooldown · entry window
09:20–15:00 · auto square-off 15:12 · kill switch · crash-restart adopts open positions.

---

## 1. Create the OCI VM

1. Sign up at cloud.oracle.com → choose **India West (Mumbai)** or **India South (Hyderabad)** as
   the **home region** (Always Free resources only exist there; you can't change it later).
   Upgrading the account to **Pay As You Go** stops Oracle reclaiming "idle" free VMs — you still
   pay ₹0 inside Always Free limits. Set a budget alert anyway.
2. **Compute → Instances → Create**
   - Image: **Canonical Ubuntu 22.04 or 24.04**
   - Shape: **VM.Standard.A1.Flex** (Ampere, 1 OCPU / 6 GB is plenty) — or **VM.Standard.E2.1.Micro**
     if A1 says "out of capacity" (setup adds swap for the 1 GB micro).
   - Networking: public subnet, assign a public IPv4 (you'll swap it for a reserved one next).
   - Add your SSH public key. Create.
3. **Reserve a static IP** (SmartAPI orders are only accepted from your registered IP):
   Networking → **IP Management → Reserved public IPs → Reserve**. Then Instance → Attached VNICs →
   VNIC → IPv4 Addresses → ⋯ on the primary private IP → **Edit** → *No public IP* → Update →
   **Edit** again → *Reserved public IP* → pick yours. Note the IP.
4. Security list: keep only **ingress TCP 22** (from your IP if possible). The bot needs no inbound ports.

## 2. Install

```bash
# from your laptop
scp -r angelbot ubuntu@<RESERVED_IP>:~/          # or git clone your private repo into ~/angelbot
ssh ubuntu@<RESERVED_IP>
cd ~/angelbot && bash deploy/setup_oci.sh        # packages, IST timezone, venv, tests, systemd timer, Claude Code
```

## 3. SmartAPI credentials + static IP

1. smartapi.angelone.in → **My Apps → Create App** (Trading API) → copy the **API key**.
   Register the VM's **reserved IP** as the app's **primary static IP** (you can change it at most
   once a week, and one IP can belong to only one client).
2. Enable **TOTP** for your Angel One account and copy the **base32 secret** (text under the QR).
3. `nano .env` → fill `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`.
   Leave `ANGELBOT_MODE=paper`. Optional: Telegram bot token + chat id for alerts.
4. Verify:
   ```bash
   ./.venv/bin/python -m bot.cli check
   # ✓ config valid · public IP (must equal your registered static IP) · login OK · funds · LTP
   ```

## 4. Data → backtest → offline replay

```bash
./.venv/bin/python -m bot.cli instruments SBIN                 # find exact symbols/tokens
./.venv/bin/python -m bot.cli fetch SBIN-EQ --days 90          # 1-min candles → data/candles/
./.venv/bin/python -m bot.cli backtest SBIN-EQ --from 2026-07-01 --to 2026-09-15
./.venv/bin/python -m bot.cli backtest SBIN-EQ --strategy orb --params '{"range_minutes":30}'
./.venv/bin/python -m bot.cli run --replay data/candles/SBIN-EQ_ONE_MINUTE.csv   # full engine, paper, offline
```

## 5. Claude Code

```bash
ssh ubuntu@<RESERVED_IP>
tmux new -A -s claude          # survives SSH disconnects
cd ~/angelbot && claude        # first run: log in, then approve the project MCP server "angelbot"
```
Slash commands (in `.claude/commands/`):

| Command | What it does |
|---|---|
| `/status` | health: alive, mode, P&L, positions, pending orders, feed, halts |
| `/eod-review [date]` | trades, signals, errors, last-10-day comparison, ≤3 testable ideas |
| `/backtest SYMBOL [strategy] [params]` | in-sample vs out-of-sample backtest table |
| `/tune SYMBOL strategy` | small grid sweep + walk-forward; proposes YAML diff only if it holds up |
| `/new-strategy <idea>` | implements a Strategy class + tests + backtest comparison |
| `/preflight` | go-live readiness checklist (read-only) |
| `/incident [what]` | root-cause errors, rejections, stuck exits, feed drops |

Or just ask: *"Why did we lose today?"*, *"Compare ORB 15 vs 30 minutes on the last 3 months"*,
*"Pause the bot, I'm seeing news"*.

Guardrails (`.claude/settings.json`): read-only tools auto-allowed; pause/resume/kill, config edits,
service restarts and candle downloads **ask you first**; `.env`, `env/printenv` and `bot/config.py`
edits are **denied**. These are guardrails, not a security boundary (Claude runs as the same Linux
user) — for hard isolation run the bot under a separate Linux user that owns `.env`.

## 6. Daily operation

- Timer starts the bot **09:05 IST Mon–Fri**; it skips holidays in `config.yaml`, flattens at 15:12, exits ~15:32.
- `./.venv/bin/python -m bot.cli status` · `journalctl --user -u angelbot -f` · `systemctl --user list-timers`
- Pause entries: `bot.cli pause` · resume: `bot.cli resume`
- **Kill switch:** `bot.cli kill` → cancels entries, exits everything with limit orders, stops.
  It then leaves the bot **paused** — run `bot.cli resume` before the next session.
- Config changes apply at next start (or `systemctl --user restart angelbot` — mid-session restarts
  re-adopt open positions from the broker).
- Update the holiday list every December.

## 6b. Telegram (alerts + control)

1. @BotFather → `/newbot` → copy the token; open your bot and tap **Start**.
2. Chat id: `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"` → `"chat":{"id":…}`.
3. `.env`: `TELEGRAM_BOT_TOKEN=…` and `TELEGRAM_CHAT_ID=…`, then
   `./.venv/bin/python -m bot.cli telegram-test` and `systemctl --user restart angelbot-telegram`.

Alerts come from the trading bot; commands are handled by the separate always-on
`angelbot-telegram` service and are accepted **only from your chat id**:
`/status` `/pnl` `/pause` `/resume` `/kill` (→ `/kill CONFIRM`) `/log` `/help`.
No order placement and no paper→live switching via Telegram, by design. Commands sent while
the service was down are discarded on restart (so a stale `/kill` can't fire later).
Never share the bot token — if leaked, `/revoke` it in BotFather.

## 7. Going live (manual, human-only)

Only after **≥ 3–4 weeks of paper trading** that roughly matches the backtest, and `/preflight` passes:
1. Set `qty: 1` (1 share / 1 lot) and a small `risk.max_daily_loss`.
2. Edit `.env` yourself:
   ```
   ANGELBOT_MODE=live
   ANGELBOT_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK
   ```
3. `bot.cli check` again (IP must match), then watch the first sessions closely with the Angel One app open.
Revert by setting `ANGELBOT_MODE=paper`.

## 8. Compliance notes (India, from 1 Apr 2026)

API orders are treated as **algo orders**. This setup is the "tech-savvy client, own algo, own
account" case: static registered IP, **LIMIT orders only** (MARKET/IOC rejected), **< 10 orders/sec**
(no exchange registration needed below that). Don't run it for other people's accounts or sell
signals from it — that needs empanelment / RA registration. Confirm specifics with Angel One SmartAPI
support; rules and broker implementations change.

## 9. Known limitations — read these

- **Stops live in the bot, not at the exchange.** If the VM/network dies with a position open, nothing
  exits it until the bot restarts (systemd restarts it in ~20s and re-adopts positions) or the broker's
  ~15:20 auto square-off. Keep size small. (An improvement: also place an exchange SL-L order per position.)
- Paper fills are optimistic vs real queues/slippage; backtests are bar-based. Expect live to be worse.
- Bundled strategies are **examples to show the plumbing, not proven edges**. Charges matter a lot at
  small sizes — most scalping strategies lose after costs.
- `charges` in config are approximations; verify against Angel One's current tariff.
- Exits use marketable LIMIT orders with chasing; in a fast market an exit can lag. Stuck exits alert
  via log/Telegram after `max_exit_chases`.

## 10. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `login failed` | wrong PIN/TOTP secret, or VM clock off → `timedatectl` (chrony should fix) |
| orders rejected, IP-related message | VM IP ≠ registered static IP → `bot.cli check`, update SmartAPI app |
| `AG8001 Invalid Token` loops | session expired (midnight) — bot re-logs in; restart service if persistent |
| no ticks / feed restarts | market closed, token wrong for exchange, or network; see `tail_log` |
| `Instrument ... not found` | symbol typo or expired contract → `bot.cli instruments <name> --exchange NFO` |
| bot didn't start | holiday, weekend, timer disabled → `systemctl --user list-timers` |
| bot started but no trades after a kill | it's paused → `bot.cli resume` |

## Layout

```
bot/            engine, strategies, risk, orders, broker, paper, feed, backtest, journal, cli
mcp_server/     MCP server for Claude Code (.mcp.json registers it)
config/         config.yaml  (strategy, instruments, risk, session, execution, charges)
deploy/         setup_oci.sh, angelbot.service, angelbot.timer
.claude/        settings.json (permissions), commands/ (slash commands)   CLAUDE.md (Claude's rules)
tests/          25 unit/integration tests (paper broker, replay engine, risk, live adapters w/ fakes)
```

**Disclaimer:** educational software, no warranty. Trading involves substantial risk of loss; most
retail intraday/F&O traders lose money. Not investment advice. You are responsible for compliance
and for every order placed with your credentials.
