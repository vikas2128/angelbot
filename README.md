# AngelBot — Angel One SmartAPI intraday bot on OCI, operated with Claude Code

```
            Oracle Cloud VM (Mumbai/Hyderabad, reserved static IP)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  systemd timer 09:05 IST Mon–Fri ─▶ angelbot@paper.service (bot.cli run)  │
 │                                                                          │
 │  SmartAPI WebSocket ─ticks─▶ BarBuilder ─▶ Strategy ─▶ RiskManager        │
 │                                   │                       │              │
 │        stops/targets on every tick│              OrderManager (LIMIT) ───┼──▶ SmartAPI REST (live)
 │                                   ▼                       ▼              │    or PaperBroker
 │   runtime/paper/: status.json  journal.db  bot.log  control.json  screen.json
 │                                                                          │
 │  angelbot-telegram@paper (always on) ◀──▶ your Paper Telegram bot        │
 │  Claude Code ──stdio──▶ mcp_server (angelbot): status, trades, SQL,      │
 │                         logs, backtests, sweeps, pause / resume / kill   │
 └──────────────────────────────────────────────────────────────────────────┘
```

**Who decides what**
- The **coded strategy + risk rules** make every real-time trading decision (deterministic, in milliseconds).
- **Claude Code** is your analyst and operator: health checks, end-of-day reviews, reports, backtests, walk-forward
  tuning, new strategies, debugging. It **cannot place orders** and **cannot switch to live**, by design.

**Safety defaults:** paper mode · LIMIT orders only · ≤ 3 orders/sec · stop loss on every trade · daily loss limit →
flatten + halt · consecutive-loss cooldown · entry window 09:20–15:00 · auto square-off 15:12 · kill switch ·
crash-restart adopts open live positions and keeps the day's trade count and loss limit.

---

## 1. Profiles (areas): what each one is for

The bot runs in separate **areas**. Each has its own folder, its own Telegram bot and its own services, so a command, a
button tap or a report in one area can never touch another. Details and the migration from the old single folder:
[docs/ENVIRONMENTS.md](docs/ENVIRONMENTS.md).

| Profile | Purpose | Files | Mode | Telegram bot | Broker login | How it runs |
|---|---|---|---|---|---|---|
| **paper** | Daily practice on **real market prices with simulated orders**. No real money. This is the one running now. | `runtime/paper/` | pinned to `paper` | Paper bot | yes | `angelbot@paper` (weekday timer) + `angelbot-telegram@paper` |
| **live** | **Real money.** Prepared but **OFF**: installed, never enabled by the setup. Only you switch it on, after the paper phase. | `runtime/live/` | you set it | Live bot | yes | `angelbot@live` + `angelbot-telegram@live` (both off) |
| **research** | Offline replays, backtests and sample data. Never places orders. | `runtime/research/` | paper, no broker | Test bot | no | by hand: `./deploy/ab research ...` |
| **test** | The unit tests (`pytest`). Uses a temporary folder. | temp folder | paper | Test bot (optional) | no | `pytest` |

- Shared by all areas: broker credentials in `.env`, market candles and stock lists in `data/`, and the code.
- Per area: `profiles/<area>.env` sets `ANGELBOT_PROFILE`, `ANGELBOT_MODE`, `ANGELBOT_RUNTIME` (its folder),
  `ANGELBOT_CONFIG` (its config file), and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` (its Telegram bot).
- `paper.env` pins `ANGELBOT_MODE=paper`, so the paper services can never trade live, whatever `.env` says.
- Config: paper and research use `config/config.yaml`; live uses `config/config.live.yaml` (you create it, with
  stricter risk limits).
- Alerts are labelled `📝 PAPER`, `🔴 LIVE` or `🧪 TEST`. A replay sends one summary to the Test bot, not one message per trade.

**Always run manual commands through the area wrapper** `./deploy/ab <area> <command>`. It loads that area's profile, so
the command reads and writes only that area's files. A bare `python -m bot.cli pause|kill|resume|unkill|screen|run` is
refused once the areas exist, because it would miss the running bot.

```bash
./deploy/ab paper status                  # = python -m bot.cli status, inside the paper area
./deploy/ab paper pause
./deploy/ab research run --replay data/candles/SBIN-EQ_ONE_MINUTE.csv
```

---

## 2. Setup

### 2.1 Create the OCI VM

1. Sign up at cloud.oracle.com and choose **India West (Mumbai)** or **India South (Hyderabad)** as the **home region**
   (Always Free resources exist only there, and it can't be changed later). Upgrading to **Pay As You Go** stops Oracle
   reclaiming idle free VMs; you still pay ₹0 inside the Always Free limits. Set a budget alert anyway.
2. **Compute → Instances → Create**
   - Image: **Canonical Ubuntu 22.04 or 24.04**
   - Shape: **VM.Standard.A1.Flex** (1 OCPU / 6 GB is plenty), or **VM.Standard.E2.1.Micro** if A1 is out of capacity
     (the setup adds swap for the 1 GB micro).
   - Networking: public subnet with a public IPv4 (swapped for a reserved one next). Add your SSH public key.
3. **Reserve a static IP** (SmartAPI accepts orders only from your registered IP): Networking → **IP Management →
   Reserved public IPs → Reserve**. Then Instance → Attached VNICs → VNIC → IPv4 Addresses → ⋯ on the primary private IP
   → **Edit** → *No public IP* → Update → **Edit** again → *Reserved public IP* → pick yours.
4. Security list: keep only **ingress TCP 22** (from your IP if possible). The bot needs no inbound ports.
   Hardening steps: [docs/HARDENING.md](docs/HARDENING.md).

### 2.2 Install

```bash
ssh ubuntu@<RESERVED_IP>
git clone <your private repo> ~/angelbot
cd ~/angelbot && bash deploy/setup_oci.sh
```

The setup installs packages, sets the IST timezone and time sync, creates `.venv`, runs the tests, copies
`.env.example` → `.env` and `profiles/*.env.example` → `profiles/*.env` if missing, installs the systemd units, and
enables **only the paper area** (`angelbot@paper.timer` and `angelbot-telegram@paper`). Live is installed as a template
but never enabled or started.

### 2.3 Broker credentials (`.env`, shared)

1. smartapi.angelone.in → **My Apps → Create App** (Trading API) → copy the **API key**. Register the VM's **reserved IP**
   as the app's **primary static IP** (changeable at most once a week; one IP belongs to one client only).
2. Enable **TOTP** on your Angel One account and copy the **base32 secret** (the text under the QR code).
3. Fill in `.env` yourself:
   ```bash
   nano .env            # ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET; leave ANGELBOT_MODE=paper
   chmod 600 .env
   ```

### 2.4 Telegram bots (one per area)

1. In Telegram, open **@BotFather** → `/newbot` three times: **Paper**, **Live**, **Test**. Copy each token, open each
   bot and tap **Start**. Two areas must never share a token.
2. Your chat id: message **@userinfobot**, or `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"` → `"chat":{"id":…}`.

### 2.5 Profile files (`profiles/<area>.env`, secret)

```bash
cp profiles/paper.env.example profiles/paper.env
cp profiles/research.env.example profiles/research.env
chmod 600 profiles/*.env
nano profiles/paper.env       # TELEGRAM_BOT_TOKEN = Paper bot, TELEGRAM_CHAT_ID = your chat id
nano profiles/research.env    # TELEGRAM_BOT_TOKEN = Test bot
```

The templates assume user `ubuntu` and the project in `~/angelbot` (systemd needs absolute paths); edit the paths if
yours differ. Put comments on their own lines (systemd keeps text after a value). Leave `profiles/live.env` uncreated
until you go live: without it the live services refuse to start.

### 2.6 Verify

```bash
./deploy/ab paper check            # config valid · public IP = registered static IP · login OK · funds · LTP
./deploy/ab paper telegram-test    # a test message from the Paper bot
./.venv/bin/python -m pytest -q    # unit tests (send nothing unless ANGELBOT_TEST_TELEGRAM_* is set in your shell)
systemctl --user list-timers 'angelbot@*' --no-pager
```

### 2.7 Configuration (`config/config.yaml`)

Strategy and its parameters, instruments, risk limits, session times and holidays, execution and charges. The optional
`screener:` and `selection:` sections are covered in §5 and §6, and `sizing.amount` (rupees per trade) in §4.

- **Which value wins:** a setting changed from Telegram (`runtime/<area>/settings.yaml`, §4) wins over `config.yaml` for
  that area. If an edit to `config.yaml` seems to have no effect, check ⚙️ Settings for a ✏️ mark.
- **Order size, in order of priority:** `sizing.amount` > 0 (shares that fit in the amount), else `selection.qty` /
  `selection.order_value` for a picked stock, else `instruments[].qty`.

- Every change: edit → `./deploy/ab paper check` (validates the config) → backtest → it applies from the **next start**
  (or a restart, below). With Claude Code: ask it, and it runs `validate_config` and a backtest first.
- Update `session.holidays` every December.

---

## 3. Start, stop and restart

The services are systemd **user** units, one instance per area (`@paper`, `@live`).

| Unit | What it is |
|---|---|
| `angelbot@paper.timer` | starts the trading bot at **09:05 IST, Mon–Fri** |
| `angelbot@paper.service` | the trading bot: one session per start; skips holidays, flattens at 15:12, exits about 15:32 |
| `angelbot-telegram@paper.service` | Telegram commands and buttons; always on, independent of trading |
| `angelbot-backtest@paper.timer` | backtests every screened stock at **15:45 IST, Mon–Fri**, for the backtest check (§5) |
| `angelbot-api@paper.service` | the phone app and its API, private over Tailscale (optional, §4b) |
| `angelbot-tlscert@paper.timer` | renews the app's HTTPS certificate daily (only with the app) |

### Paper

```bash
# trading bot
systemctl --user start angelbot@paper          # start now (exits at once on a weekend, holiday or after the session)
systemctl --user restart angelbot@paper        # mid-session restart: only with NO open paper position (see below)
systemctl --user stop angelbot@paper           # stop now (prefer a pause or kill below when positions are open)
systemctl --user status angelbot@paper
journalctl --user -u angelbot@paper -f         # follow the log

# the morning timer
systemctl --user enable --now angelbot@paper.timer
systemctl --user disable --now angelbot@paper.timer   # no automatic start tomorrow

# Telegram controller
systemctl --user restart angelbot-telegram@paper
systemctl --user status angelbot-telegram@paper
journalctl --user -u angelbot-telegram@paper -n 50 --no-pager
```

Trading controls (they act on the running bot through `runtime/paper/control.json`):

```bash
./deploy/ab paper status
./deploy/ab paper pause        # no new entries; open trades keep their stops
./deploy/ab paper resume       # allow entries again; also clears a kill
./deploy/ab paper kill         # emergency: cancel entries, exit everything with limit orders, stop (and stay paused)
./deploy/ab paper unkill       # clear only the kill flag; the bot stays paused until resume
```

### Live (only after you have gone live yourself, §8)

The same commands with `@live` and `./deploy/ab live ...`. Until `profiles/live.env` exists they fail with
"Failed to load environment files", which is intended. Never enable `angelbot@live.timer` before going live.

### After a `git pull` (new code)

```bash
cd ~/angelbot && git pull
systemctl --user restart angelbot-telegram@paper   # always: it is long-running and keeps the old code otherwise
systemctl --user daemon-reload                     # only if files in deploy/*.service or *.timer changed, after copying
                                                   # them again: cp deploy/angelbot*@.* ~/.config/systemd/user/
# first time after this update only: install and switch on the nightly per-stock backtests
cp deploy/angelbot-backtest@.* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now angelbot-backtest@paper.timer
```

The trading bot loads new code at its next start (tomorrow's 09:05 timer). To use it today, outside a position,
`systemctl --user restart angelbot@paper`. Config changes work the same way.

**Restarting mid-session.** Today's closed trades are rebuilt from the journal, so the trade count, daily loss limit,
loss streak and cooldown carry on, and today's picked stocks are kept. Open positions differ by area:
- **paper:** a simulated position exists only in the bot's memory and is **lost** on a restart (it is never closed in
  the journal). Restart only with no open position: `./deploy/ab paper status`, or use 🔄 Restart in Telegram, which
  checks this for you.
- **live:** open positions are read back from the broker and managed again, with the stop and target set from
  the average price.

---

## 4. Telegram

Alerts come from the trading bot: start and morning heartbeat, trade opened and closed (with times), stuck exits,
rejected orders, feed lost and back, loss cooldown, end-of-day summary. Commands are handled by
`angelbot-telegram@<area>`, accepted **only from your chat id**, and act only on that area:

| Command / button | What it does |
|---|---|
| `/status` · 📊 Status | running or not, today's result, open position, and what the strategy sees per stock (ema_cross: both averages and the 1-minute close that would give the next signal; orb: range and break levels; vwap_revert: VWAP and bands) |
| `/pnl` (`/summary`) · 💰 Today | today's trades and result |
| `/pause` · `/resume` | stop / allow new entries |
| `/kill` · 🛑 Emergency stop | close everything and stop; asks for confirmation, valid for 60 seconds |
| `/report [all\|session\|replay] [YYYY-MM-DD]` · 📄 Report | standalone HTML report with **Today** and **Month** tabs, sent as a file |
| `/screen` · 🔎 Screener | latest screener picks plus the full ranking as an HTML file (no broker calls) |
| `/settings` · ⚙️ Settings | change selection, shares, universe, strategy, risk and exits (below) |
| `/log` · `/menu` · `/help` | last 20 log lines, buttons, help |

No order placement and no paper → live switching through Telegram, by design. Commands sent while the controller was
down are discarded on restart, so a stale `/kill` can't fire later. If a token leaks, `/revoke` it in BotFather.
The report and screener files work without JavaScript; if a tab does not respond in Telegram's preview, open the file in
your phone's browser.

### Settings from Telegram (⚙️ Settings)

Tap a setting, then a value. Only fixed choices are offered, no typed values:

| Setting | Choices |
|---|---|
| Amount per trade (₹ per trade) | off · ₹2,000 · 5,000 · 10,000 · 20,000 · 25,000 · 50,000, or type `/amount 7500` |
| Selection mode / picks to trade | off · auto · confirm / 1 · 2 · 3 |
| Shares for a picked stock / for my list | 1 · 2 · 5 · 10 · 20 · 50 |
| Stocks to screen | built-in 20 · Nifty 50 · Bank Nifty · Nifty 50 + Bank Nifty |
| Backtest check | off · on (skip stocks whose recent backtest lost, §5) |
| Strategy | ema_cross · orb · vwap_revert |
| Max daily loss / max trades per day | ₹250 to ₹5,000 / 3 · 5 · 10 · 20 |
| Stop loss / target | 0.2–1.0% / 0.3–2.0% |

- **Amount per trade** (`sizing.amount` in config, or `/amount 7500` in Telegram, `/amount off` to stop): at each entry
  the bot buys as many whole shares as fit in the amount at the entry's limit price, so one order never costs more
  than it. A stock whose one share costs more is skipped (and the screener leaves it out). While an amount is set, the
  share counts are not used. It must be at most `risk.max_order_value`. Fees are a bigger share of a small order.
- Changes are saved in `runtime/<area>/settings.yaml`, **not** in `config/config.yaml`, and win over it for that area
  only: a change in the Paper bot never touches research or live. ✏️ marks a changed value; **↩️ Reset all** goes back
  to config.yaml. The file keeps the last 20 changes.
- Every change is validated on the whole config first; an invalid one is refused and nothing is saved.
- Changes apply from the **next start**. The start alert lists the settings changed in Telegram.
- **🔄 Restart bot now** (paper only) applies them at once. It is offered only while the bot runs with **no open position
  and no waiting order** (a paper position lives only in the bot's memory), asks to confirm within a minute, and checks
  again before restarting. After a restart today's picked stocks are kept (a new mode or number of picks fully applies
  tomorrow). The day's closed trades still count: at start the bot rebuilds today's trade count, P&L after fees,
  loss streak and cooldown from the journal (also after a crash restart), and the start alert says so.
- Not changeable here, on purpose: paper/live mode, orders per second, order type, the order cap, session times.
- Claude sees the overrides too: the `get_config` and `validate_config` tools list them.

---

### 4b. Phone app (optional)

An installable app for your phone with the same information and controls as Telegram, live updates and push
notifications. It runs on the VM and is reachable **only over Tailscale** (no new internet-facing port). Sign-in is by
passkey (Face ID or fingerprint); each action asks again if your last check is over 2 minutes old; it cannot place
orders or switch to live. Telegram stays on as the backup.

```bash
sudo tailscale up --advertise-tags=tag:angelbot      # after setting up your Tailscale account: docs/APP.md §1
bash deploy/app_setup.sh                              # one time: certificate, service, prints the app's address
./deploy/ab paper app enroll --name "My phone"        # a one-time code to register the phone
```

Then open the printed `https://<vm>.<tailnet>.ts.net:8443` on the phone (Tailscale on), register, and add it to the
home screen. Full guide, lost-phone steps and troubleshooting: [docs/APP.md](docs/APP.md).

## 5. Stock screener (advisory)

The screener ranks liquid stocks for intraday trading and writes `runtime/<area>/screen.json` (plus an HTML page if
asked). It reads market data only (daily candles and one batched quote call) and **never places orders**. Unless
selection is on (§6), the bot still trades only the instruments in the config.

- **Gates** (a stock failing any is left out, with the reason): average traded value ≥ ₹100 cr, bid-ask spread ≤ 0.10%,
  daily range ≥ 5× round-trip fees at the planned order size, one order within `risk.max_order_value`, at least 10
  days of history. With selection on, the order size is the bot's real one (`selection.qty`, e.g. 5 shares, or
  `selection.order_value`); otherwise about ₹40,000.
- **Score** (0 to 100) among the stocks that pass: room to move versus fees, today's gap, how much of a normal day's
  volume has traded so far, and liquidity. A missing factor is left out, never guessed.
- The ranking is a heuristic and has **not been backtested**. Use it to shortlist stocks to test, not as a signal.

### Run it manually

Run it **before 09:00 or after about 15:32**, when that area's bot is not running. The screener logs in to SmartAPI on
its own, and a second login beside a running bot has not been tested, so it refuses while the bot is alive unless you
add `--force`. It paces calls at one a second: the Nifty 50 + Bank Nifty lists (about 59 stocks) take about a minute.

**Paper**
```bash
./deploy/ab paper screen                                   # the universe from config (or the built-in 20 stocks)
./deploy/ab paper screen --universe nifty50,banknifty      # members of both indices
./deploy/ab paper screen --universe SBIN-EQ,TCS-EQ --top 3 --html runtime/paper/screen.html
```
Then tap 🔎 Screener (or send `/screen`) in the Paper bot.

**Live** (once `profiles/live.env` and `config/config.live.yaml` exist)
```bash
./deploy/ab live screen --universe nifty50,banknifty
```
It reads the same market data and writes to `runtime/live/screen.json`, shown by the Live bot. It never trades, but
the same rule applies: not while the live bot is running. Both areas use the same broker account, so don't screen in
one area while the other area's bot is running either.

### Backtest check (only stocks whose recent backtest made money)

With `screener.backtest_gate: true` (on in the shipped config; ⚙️ Settings → Backtest check):

1. **Every weekday at 15:45** `angelbot-backtest@paper.timer` backtests each stock the screener looks at, with the
   current strategy, exits, risk limits and order size (amount per trade or shares), over the last 30 days of 1-minute
   candles. Results: `runtime/paper/stock_backtests.json`. About 3 seconds a stock (Nifty 50 + Bank Nifty: about 3 minutes).
2. **Next morning** the screener leaves out a stock whose backtest lost money after fees, or made fewer than
   `bt_min_trades` (20) trades, with the reason. The page and the Telegram summary show each pick's backtest result.
3. **No usable backtest** (none yet, older than 4 days, or the strategy, exits, risk limits or order size changed
   since): every stock is left out and the page says why, so morning selection falls back to the instruments in your
   config (`selection.on_failure`). Re-run it by hand after changing those settings.

```bash
./deploy/ab paper backtest-stocks                          # by hand, after the close (refuses while the bot runs)
./deploy/ab paper backtest-stocks --universe nifty50 --days 30
systemctl --user start angelbot-backtest@paper             # the same through systemd
journalctl --user -u angelbot-backtest@paper -n 80 --no-pager
```

**Strategy per stock (`screener.strategy_per_stock: true`, on in the shipped config; ⚙️ Settings → Per-stock strategy).**
The nightly job also runs a walk-forward check per stock: each strategy is backtested on the older two-thirds of the 30
days and on the newer third separately. A stock's strategy is the one with the best result after fees on the older
days, kept only if it **also** made money on the newer days, with at least 12 and 5 trades. A stock where no strategy
passes is left out of the ranking. Each picked stock then trades its own strategy (the trade journal records which);
`/status`, the app and the morning message show it. With `selection.top_n: 5` and `risk.max_open_positions: 5` up to
five stocks trade at once, sharing `risk.max_daily_loss` and `max_trades_per_day`. If no stock passes, selection
follows `selection.on_failure` (`config`: your instruments list with the default strategy; `none`: no trading).

**Strategy comparison (information only).** The same nightly run also backtests every strategy (ema_cross, orb,
vwap_revert) on each stock, with the same order size, exits and limits. The screener page gets a **Strategies** tab
(totals per strategy: net after fees, trades, win rate, on how many stocks it made money, and a per-stock table),
the Telegram summary lists the totals, and `backtest-stocks` prints them. It **never switches the strategy**: that
stays your choice in ⚙️ Settings → Strategy. Before switching, confirm the better one on a later period it was not
chosen on (`./deploy/ab research backtest SYMBOL --strategy orb --from ... --to ...`), then paper trade it.

Be honest about what it shows: 30 days is a few dozen trades per stock, and picking the stocks that did well last
month is choosing on past data. It filters out stocks that clearly lose with this strategy; it does not prove a pick
will make money. Judge it by comparing paper results with and without the check over several weeks.

### Nifty 50 and Bank Nifty lists

Stocks only (no index futures or options). The lists come from NSE's official constituent CSVs: a snapshot ships in
`config/universe/`, and a refresh downloads fresh copies into `data/universe/` (git-ignored, shared by all areas; a
refreshed copy wins). A refresh is validated and never replaces a good list with a broken one. Membership changes at
NSE's index reviews, so refresh now and then; a list older than about four months is flagged on the screener page.

```bash
./deploy/ab paper universe show          # count and age of each list
./deploy/ab paper universe refresh       # both lists (or --name nifty50 / --name banknifty)
```

Optional `screener:` keys in the config override the defaults, for example:

```yaml
screener:
  universe: [nifty50, banknifty]   # presets and/or symbols such as SBIN-EQ
  top_n: 3
  order_value: 50000               # planned order size in rupees, used by the fee gate
```

---

## 6. Morning stock selection (opt-in, off by default)

With `selection.mode: off` (the default, also when the section is absent) the bot trades exactly the instruments in the
config. To let the screener choose what the bot trades each day, add this to the config (with your approval, then
validate; it applies from the next session):

```yaml
selection:
  mode: confirm          # off | auto | confirm
  top_n: 1               # how many of the top picks to trade (1 to 3)
  screen_at: "09:16:30"  # after the open, so today's gap exists
  confirm_until: "09:19:30"   # at or before session.entry_start
  on_timeout: config     # confirm with no tap: config (your list) | top (the top picks)
  on_failure: config     # screen failed or nothing passed: config | none (no trading today)
  qty: 1                 # shares per order for a picked stock ...
  order_value: 0         # ... or, if > 0, shares = this many rupees // price (at least 1 share)
```

- **auto**: at `screen_at` the bot screens on its own login (no second login) and trades the top `top_n` picks.
- **confirm**: it sends the picks to Telegram with buttons (*Only X*, *Trade top N*, *Keep my list*) and waits until
  `confirm_until`. A tap applies at once; no tap follows `on_timeout`.
- A tap can only choose a stock that passed the gates in today's snapshot, and only while the bot is waiting.
- The decision is saved for the day, so a restart keeps the same stocks. Any failure falls back per `on_failure`.
- Trades still follow the same strategy, stops and risk limits. A bigger `order_value` raises the loss per stop against
  `risk.max_daily_loss`. Run it in paper and compare against a fixed list before trusting it.

---

## 7. Research, backtests and reports

```bash
./deploy/ab research instruments SBIN                         # exact symbols and tokens
./deploy/ab research fetch SBIN-EQ --days 90                   # 1-minute candles → data/candles/ (logs in for market data; not while a bot runs)
./deploy/ab research backtest SBIN-EQ --from 2026-07-01 --to 2026-09-15
./deploy/ab research backtest SBIN-EQ --strategy orb --params '{"range_minutes":30}'
./deploy/ab research backtest SBIN-EQ --amount 10000          # shares per trade = what fits in ₹10,000 (or --qty 5)
./deploy/ab research run --replay data/candles/SBIN-EQ_ONE_MINUTE.csv    # full engine, offline, one summary to the Test bot
./deploy/ab research sample-data SBIN-EQ --days 20             # synthetic candles (prove nothing about returns)
```

Report for any agent or by hand (standard library only, see `skills/angelbot-report/SKILL.md`):

```bash
python3 skills/angelbot-report/build_report.py --db runtime/paper/journal.db --out report.html
```

Fewer than 100 trades, or synthetic data, prove nothing. Judge results on net P&L after charges, trade count, win
rate, profit factor and max drawdown, and confirm tuning on a later, unseen window (walk-forward).

---

## 8. Claude Code

On the VM:
```bash
tmux new -A -s claude          # survives SSH disconnects
cd ~/angelbot && claude        # first run: log in, then approve the project MCP server "angelbot" (paper area)
```

From your laptop or Claude Desktop, the same MCP server can run over SSH; see [docs/HARDENING.md](docs/HARDENING.md)
for the restricted key setup.

| Command | What it does |
|---|---|
| `/status` | health: alive, mode, P&L, positions, pending orders, feed, halts |
| `/eod-review [date]` | trades, signals, errors, last-10-day comparison, up to 3 testable ideas |
| `/report` | performance dashboard from the journal |
| `/backtest SYMBOL [strategy] [params]` | in-sample versus out-of-sample backtest table |
| `/tune SYMBOL strategy` | small grid sweep + walk-forward; proposes a YAML diff only if it holds up |
| `/new-strategy <idea>` | a Strategy class + tests + backtest comparison |
| `/preflight` | go-live readiness checklist (read-only) |
| `/incident [what]` | root cause of errors, rejections, stuck exits, feed drops |

Guardrails (`.claude/settings.json`): read-only tools are allowed; pause, resume, kill, config edits, service restarts
and candle downloads ask you first; `.env`, `profiles/*.env`, and starting or enabling the live services are denied.
These are guardrails, not a security boundary (Claude runs as the same Linux user). For hard isolation, run the bot
under a separate Linux user that owns `.env`.

---

## 9. Going live (manual, human-only)

Only after **3–4 weeks of paper trading** that roughly matches the backtest, and `/preflight` passes:

1. `cp config/config.yaml config/config.live.yaml` and set `qty: 1` (1 share or 1 lot) and a small `risk.max_daily_loss`.
2. `cp profiles/live.env.example profiles/live.env && chmod 600 profiles/live.env`, then add the Live bot token and
   uncomment the two lines yourself:
   ```
   ANGELBOT_MODE=live
   ANGELBOT_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK
   ```
3. `./deploy/ab live check` (the IP must match), then stop paper from trading on the same account and enable live:
   ```bash
   systemctl --user disable --now angelbot@paper.timer
   systemctl --user enable --now angelbot@live.timer angelbot-telegram@live
   ```
4. Watch the first sessions with the Angel One app open.

To revert: `systemctl --user disable --now angelbot@live.timer` and comment out the two lines in `profiles/live.env`
(the bot then runs in paper mode inside the live folder).

---

## 10. Compliance notes (India, from 1 Apr 2026)

API orders are treated as **algo orders**. This setup is the "tech-savvy client, own algo, own account" case: static
registered IP, **LIMIT orders only** (MARKET/IOC rejected), **under 10 orders/sec** (no exchange registration needed
below that). Don't run it for other people's accounts or sell signals from it; that needs empanelment or RA
registration. Confirm specifics with Angel One SmartAPI support; rules and broker implementations change.

## 11. Known limitations

- **Stops live in the bot, not at the exchange.** If the VM or network dies with a position open, nothing exits it
  until the bot restarts (systemd retries in about 20 s and re-adopts positions) or the broker's ~15:20 auto
  square-off. Keep size small.
- Paper fills are optimistic versus real queues and slippage; backtests are bar-based. Expect live to be worse.
- The bundled strategies are **examples of the plumbing, not proven edges**. Charges dominate at small sizes: at 1 share
  the round trip costs about 0.27% of the trade.
- `charges` in the config follow Angel One's published NSE intraday tariff (checked 23 Oct 2026: brokerage lower of ₹20
  or 0.1%, minimum ₹5, per executed order; NSE 0.0030699% + IPFT; SEBI ₹10/crore; STT 0.025% on sells, rounded to the
  rupee; stamp 0.003% on buys; GST 18%). Contract notes round STT per stock per day, the bot per sell order (at most
  ₹0.50 apart). Re-check the tariff page when Angel One announces a change (they give 30 days' notice). The ₹5
  minimum makes very small orders expensive: 1 share at ₹800 costs about 1.5% a round trip.
- Exits use marketable LIMIT orders with chasing; in a fast market an exit can lag. Stuck exits alert after
  `max_exit_chases`.
- The screener, morning selection and any value changed in ⚙️ Settings have not been backtested. Backtest a new size
  or limit first (`backtest --amount` / `--qty`, §7).
- A **paper** position is lost if the bot restarts while it is open (crash or manual restart); live positions are
  re-adopted from the broker.

## 12. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `login failed` | wrong PIN or TOTP secret, or VM clock off → `timedatectl` |
| orders rejected, IP-related message | VM IP ≠ registered static IP → `./deploy/ab paper check`, update the SmartAPI app |
| `Failed to load environment files` | `profiles/<area>.env` missing: copy it from the `.example` (intended for live until you go live) |
| `No area given, so '...' would use the old runtime/ folder` | use `./deploy/ab <area> <command>` instead of a bare `bot.cli` command |
| `screen` refuses: bot is running | run it after the session (about 15:32) or before 09:00 |
| a config.yaml change has no effect | a Telegram setting overrides it: ⚙️ Settings shows ✏️; tap ↩️ Reset all |
| 🔄 Restart says "Restart failed" | run `systemctl --user restart angelbot@paper` on the VM |
| every stock "left out: backtest check: ..." | no fresh per-stock backtest: `./deploy/ab paper backtest-stocks` after the close |
| Telegram buttons show old behaviour | `systemctl --user restart angelbot-telegram@paper` after a `git pull` |
| "No screener snapshot" in Telegram | no screen run yet in that area → `./deploy/ab paper screen` |
| `AG8001 Invalid Token` loops | session expired (midnight); the bot re-logs in, restart the service if it persists |
| `429 ... Connection Limit Exceeded` on the feed | too many live websocket connections for your client id: something else (another script, a second bot) holds one; the bot keeps one connection and backs off (30 s up to 5 min) while the broker refuses |
| no ticks / feed restarts | market closed, wrong token for the exchange, or network → `journalctl --user -u angelbot@paper` |
| `Instrument ... not found` | symbol typo or expired contract → `./deploy/ab paper instruments <name> --exchange NFO` |
| bot didn't start | holiday, weekend or timer disabled → `systemctl --user list-timers 'angelbot@*'` |
| no trades after a kill | it is paused → `./deploy/ab paper resume` before the next session |

## Layout

```
bot/            engine, strategies, risk, orders, broker, paper, feed, backtest, journal, cli,
                screener + screen_data + screen_report, universe, selection, telegram_ctl, notify, messages
mcp_server/     MCP server for Claude Code (.mcp.json registers it, on the paper area)
config/         config.yaml (strategy, instruments, risk, session, execution, charges); universe/ (NSE index lists)
profiles/       paper / live / research templates (*.env.example); the real *.env files are secret and git-ignored
deploy/         setup_oci.sh, angelbot@.service/.timer, angelbot-telegram@.service, angelbot-backtest@.service/.timer,
                angelbot-api@.service, angelbot-tlscert@.service/.timer, app_setup.sh, app_cert.sh, ab (area wrapper),
                split_journal.py
app/            the phone app (installable web app): screens, service worker, icons
skills/         angelbot-report: portable report builder for any agent
docs/           ENVIRONMENTS.md (areas), HARDENING.md (VM security), APP.md (phone app), APP_PLAN.md (its design)
.claude/        settings.json (permissions), commands/ (slash commands)   CLAUDE.md (Claude's rules)
tests/          unit and integration tests (pytest)
```

**Disclaimer:** educational software, no warranty. Trading involves substantial risk of loss; most retail intraday and
F&O traders lose money. Not investment advice. You are responsible for compliance and for every order placed with
your credentials.
