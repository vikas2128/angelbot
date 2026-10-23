# Build plan: AngelBot app (installable web app + private API over Tailscale)

Status: **phases 0 to 3 built** (shared actions, alert outbox, read-only API, passkey login); phases 4 to 7 not built.
The API is not installed as a service yet. Telegram stays on as the backup alert and control channel.

Built so far:
- `bot/actions.py`: pause, resume, kill, paper restart and settings in one place (Telegram and Claude's MCP use it).
- `bot/alerts.py`: `notify.send` writes every alert to `runtime/paper/alerts.jsonl`.
- `bot/api_server.py` (`./deploy/ab paper api`): GET-only data routes and a live WebSocket; paper area only;
  Tailscale-or-loopback bind check; HTTPS and a `.ts.net` name required off `--dev`; rate limit; security headers.
- `bot/api_auth.py`: passkeys only (py_webauthn), user verification required; one-time enrolment codes made on the VM
  (`./deploy/ab paper app enroll --name ...`, 10 minutes, one use, stored hashed); 12-hour sessions in an HttpOnly,
  Secure, SameSite=Strict cookie, stored hashed; step-up within 2 minutes, bound to the session's own device; lockout
  after 5 failures for 15 minutes (kept in memory: an API restart clears it); every POST needs the app's Origin and an
  `X-Requested-With: angelbot` header, a JSON object body under 64 KB with only the expected fields; audit log in
  `runtime/paper/app/audit.jsonl`; Telegram alerts for a new enrolment code, a new or removed device, a lockout and
  "sign out everywhere". `app devices`, `app remove --id`, `app signout-all` on the VM.
- Tests use a software passkey that signs real WebAuthn responses, so the library's verification runs in full.

## 1. Goal

A home-screen app on your phone (an installable web app, "PWA") that shows the paper bot live and lets you do what
Telegram does today, with push notifications. It runs on the OCI VM next to the bot, is reachable **only over
Tailscale**, and can never place orders or switch to live.

```
Phone (home-screen app) ──Tailscale (WireGuard, private)──▶ OCI VM
                                                            ├─ angelbot-api@paper       NEW: screens + REST + WebSocket + push
                                                            ├─ angelbot@paper           trading (unchanged)
                                                            ├─ angelbot-telegram@paper  backup alerts and controls (unchanged)
                                                            └─ runtime/paper/           the files all three share
```

### What the app does (version 1)

| Screen | Shows / does | Reuses |
|---|---|---|
| **Home** | live: running?, mode, today's P&L after fees, trades, open position (price, stop, target), feed health, strategy state ("buy if a minute closes above ₹…"), halts, cooldown | `status.json` (written every ~2 s), `messages.strategy_state_lines` logic |
| **Trades** | today's closed trades with times, fees and net; month totals | journal (read-only), `skills/angelbot-report/build_report.py` |
| **Report** | the existing two-tab HTML report | `build_report_file` |
| **Screener** | the existing screener page (picks, Strategies tab, left-out reasons) | `screen.json`, `screen_report.render_html` |
| **Settings** | the same 12 settings with the same fixed choices, amount per trade, reset | `bot/settings.py` (validation unchanged) |
| **Controls** | pause, resume, emergency stop (confirm), restart bot (paper only, same safety checks) | `control.set_control`, `telegram_ctl.restart_refusal` |
| **Alerts** | push notifications for every bot alert, and an in-app list of recent ones | new alert outbox (below) |

### What it never does

No order placement, modification or cancellation. No paper→live switch. No access to the live area, `.env`,
`profiles/*.env` or broker credentials (the API never logs in to the broker). No shell, file or SQL endpoints.

## 2. Architecture

### New files
```
bot/actions.py           pause / resume / kill / restart / settings: the ONE place these live; Telegram and the API both call it
bot/alerts.py            alert outbox: notify.send() also appends each alert to runtime/<area>/alerts.jsonl
bot/api_server.py        FastAPI app: REST, WebSocket, static files, auth, push sender
bot/api_auth.py          passkeys (WebAuthn), sessions, device list, enrolment codes, step-up checks, lockout
bot/api_push.py          web push (VAPID): subscriptions, send, prune dead subscriptions
app/                     the web app: index.html, app.js, app.css, manifest.webmanifest, sw.js (service worker), icons
deploy/angelbot-api@.service
docs/APP.md              setup and use (written at the end)
tests/test_api.py, tests/test_api_auth.py, tests/test_alerts.py
```

### Changed files (small)
- `bot/telegram_ctl.py`: pause, resume, kill, restart and settings call `bot/actions.py`, so both channels behave the
  same. Behaviour unchanged, tests unchanged.
- `bot/notify.py`: `send()` also writes the alert to the outbox (never blocks, never fails the bot).
- `bot/cli.py`: `app enroll` (one-time code to register a device), `app devices` (list, remove), `app keys` (create or
  rotate secrets).
- `requirements.txt`: `fastapi`, `uvicorn`, `webauthn`, `pywebpush`, pinned to exact versions with hashes.
- `deploy/setup_oci.sh`, `README.md`, `docs/HARDENING.md`.

### Data flow
- **Live status:** the API watches `runtime/paper/status.json` and pushes changes to open WebSockets about every
  2 seconds. The trading bot is not touched.
- **Alerts:** the bot appends each alert to `alerts.jsonl` (the same text Telegram gets). The API tails it and sends
  web push to registered devices. The bot never waits on push, so a push outage cannot slow trading.
- **Actions:** the API calls `bot/actions.py`, which writes `control.json` / `settings.yaml` exactly as Telegram does
  now. The bot picks them up within about a second, as today.

### REST endpoints (all need a session; actions also need step-up)

| Method | Path | What |
|---|---|---|
| GET | `/api/status` | `status.json` + control flags |
| GET | `/api/trades?date=` | closed trades and totals (read-only journal) |
| GET | `/api/report` | report HTML |
| GET | `/api/screener` | screener HTML + summary |
| GET | `/api/settings` | current values, choices, overrides, problems |
| GET | `/api/alerts?since=` | recent alerts |
| POST | `/api/control/pause`, `/resume` | step-up |
| POST | `/api/control/kill` | step-up, plus a second confirm request within 60 s |
| POST | `/api/control/restart` | step-up; paper only; refused with an open position or waiting order |
| POST | `/api/settings/{id}` | step-up; body `{"choice": n}` or `{"amount": 7500}`; same validation as Telegram |
| POST | `/api/settings/reset` | step-up |
| WS | `/ws` | live status stream (session cookie checked on connect, origin checked) |
| — | `/auth/*` | passkey register / login / step-up, logout, device list |

## 3. Security, layer by layer

### Layer 1: network
- The API binds **only to the VM's Tailscale address** (100.x.y.z), never `0.0.0.0`. A startup check refuses to run
  if the configured address is not a Tailscale address.
- No change to the OCI security list: still only SSH open to the internet.
- HTTPS with Tailscale's certificate (`tailscale cert`) on `https://<vm>.<tailnet>.ts.net`, renewed by a timer.
  Plain HTTP is refused.
- Tailscale access rule (ACL): only your devices may reach `tag:angelbot:443`, nothing else on the VM.
- Later, optional: move SSH behind Tailscale and close public port 22, keeping the OCI serial console as the way in.

### Layer 2: accounts and devices (done by you in the Tailscale admin console)
- 2-factor login or a passkey on the account you sign in to Tailscale with.
- **Device approval** on, **Tailnet Lock** on, key expiry kept on.
- A screen lock (Face ID, fingerprint or PIN) on the phone.
- Lost phone: remove it in Tailscale, then `./deploy/ab paper app devices remove <name>`.

### Layer 3: app login
- **Passkeys only** (WebAuthn: Face ID / fingerprint). No passwords.
- **Enrolment needs the VM:** a new device can register only with a one-time code from
  `./deploy/ab paper app enroll` (valid 10 minutes, one use). So someone on your tailnet still cannot add a device.
- **Sessions:** random token, stored server side, sent as a cookie with `HttpOnly; Secure; SameSite=Strict`,
  12-hour lifetime, ended by logout, device removal or key rotation.
- **Step-up for every action:** pause, resume, kill, restart and settings each need a fresh Face ID check (passkey
  assertion less than 2 minutes old). Viewing does not.
- **Lockout:** 5 failed logins from a device locks login for 15 minutes; each lockout sends a Telegram alert.

### Layer 4: what the API allows
- A fixed list of endpoints (section 2). No generic file, shell, SQL or broker access, and tests prove the order and
  live endpoints do not exist.
- Area-locked: `angelbot-api@paper` reads and writes only `runtime/paper/`; it refuses to start for `live`.
- Every request body is checked against a strict schema; unknown fields are rejected. Settings go through
  `bot/settings.py`, so the same choices and validation apply as in Telegram.
- Rate limits: 30 reads per minute and 10 actions per minute per device.
- Responses never contain tokens, credentials or file paths.
- Browser protections: CSP `default-src 'self'` (no outside scripts or fonts), `frame-ancestors 'none'`, HSTS,
  no CORS (same origin only), WebSocket origin check, and CSRF protection through `SameSite=Strict` plus an
  `X-Requested-With` header check.

### Layer 5: the VM
- Runs as your normal user, never root, with systemd hardening: `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome=read-only` with write access only to `runtime/paper/` and the app's secrets folder, `PrivateTmp`,
  `RestrictAddressFamilies`, memory and CPU limits.
- The API never reads `.env` or `profiles/*.env` beyond the paper profile's area paths (it loads the profile for
  `ANGELBOT_RUNTIME`; the Telegram token in it is not used by the API).
- Automatic security updates (`unattended-upgrades`), `ufw` and fail2ban per `docs/HARDENING.md`.
- Python dependencies pinned with hashes; `pip-audit` in the test run.

### Layer 6: secrets
- Created on the VM by `./deploy/ab paper app keys`: session signing key, VAPID push keys, the device list.
  Stored in `runtime/paper/app/` (git-ignored) with `chmod 600`. `app keys --rotate` replaces them and signs
  everyone out.
- Nothing secret in logs. Passkey public keys only are stored (WebAuthn never shares the private key).

### Layer 7: audit and alerts
- An audit log (`runtime/paper/app/audit.jsonl` and the service journal): every login, step-up, action, settings
  change, failed attempt and lockout, with device name and time.
- **Telegram tells you about the app:** a new device registered, a lockout, an emergency stop or restart from the app,
  and the API starting or stopping. This is a separate channel, so you would notice even if the app were misused.

### Layer 8: backup and recovery
- Telegram keeps every alert and control it has today. If Tailscale, the app or push fails, nothing is lost.
- `systemctl --user stop angelbot-api@paper` shuts the app off completely; trading and Telegram carry on.
- The app is read-mostly: if its files are deleted, the bot is unaffected.

## 4. Build phases

| Phase | Work | Done when |
|---|---|---|
| **0. Shared actions** | move pause / resume / kill / restart / settings into `bot/actions.py`; Telegram calls it | all current Telegram tests pass unchanged |
| **1. Alert outbox** | `notify.send()` also appends to `alerts.jsonl`, capped and rotated | the bot never fails or slows if writing fails (tested) |
| **2. API, read-only** | FastAPI app, Tailscale-only bind check, status / trades / report / screener / settings GET, WebSocket | tests for every endpoint; refuses a non-Tailscale address |
| **3. Auth** | passkey enrolment with one-time code, login, sessions, step-up, lockout, device list, audit, Telegram security alerts | tests: no session → 401, stale step-up → 403, used or expired code refused, lockout after 5 tries |
| **4. Actions** | pause / resume / kill (two-step) / restart / settings through `bot/actions.py` | same refusals as Telegram (open position, live area), rate limits |
| **5. Web app** | screens, live updates, install manifest, service worker, offline "can't reach the bot" screen, dark mode | works at phone width; Lighthouse PWA check; CSP has no violations |
| **6. Push** | VAPID keys, subscribe from the app, send from the outbox, prune dead subscriptions | a test alert arrives on Android and on iPhone (iOS 16.4+, home-screen app) |
| **7. Deploy** | systemd unit with hardening, cert renewal timer, setup script, `docs/APP.md`, README | fresh install from the docs works end to end |

Rough effort: about 4 to 6 working days in total. Phases 0 to 4 can be tested fully on a laptop; 5 needs a phone
browser; 6 and 7 need the VM and Tailscale.

## 5. Testing

- Unit and API tests for every endpoint, auth rule and refusal (session, step-up, enrolment, lockout, rate limits,
  schema rejection, area lock).
- **Guardrail tests:** no route places or cancels orders, switches mode, reads `.env`, or touches `runtime/live/`;
  every POST without step-up is refused.
- The existing 189 tests keep passing (Telegram and the bot unchanged in behaviour).
- Manual on the VM: install, enrol a phone, receive a push for a paper trade, pause from the app and see the bot
  obey, emergency stop with confirmation, lost-phone removal, `systemctl stop` of the API leaves trading untouched.

## 6. What you do yourself (about 15 minutes)

1. Create a Tailscale account with 2-factor login; install Tailscale on the VM (`curl -fsSL https://tailscale.com/install.sh | sh`
   then `sudo tailscale up`) and the Tailscale app on your phone.
2. In the admin console: turn on device approval, Tailnet Lock, MagicDNS and HTTPS certificates; tag the VM
   `tag:angelbot`; paste the access rule from `docs/APP.md`.
3. On the VM: `git pull`, `./deploy/ab paper app keys`, enable `angelbot-api@paper`.
4. On the VM: `./deploy/ab paper app enroll` and note the code. On the phone: open
   `https://<vm>.<tailnet>.ts.net`, enter the code, register Face ID, tap Add to Home Screen, allow notifications.

## 7. Risks and open points

- **iPhone push** works only for a home-screen web app on iOS 16.4 or later, and notifications can be delayed when
  the phone is in Low Power mode. Telegram remains the reliable fallback for critical alerts.
- **Tailscale outage:** the app is unreachable; Telegram still works.
- **Passkeys need HTTPS** on the `ts.net` name; the certificate must renew (a timer does it, and a failure alerts on
  Telegram).
- **Live area:** out of scope for version 1 on purpose. A live version would need its own review.
- New Python dependencies (FastAPI, uvicorn, webauthn, pywebpush) add code to keep updated; pinned and audited.
