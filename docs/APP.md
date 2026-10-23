# The AngelBot app: install and use

A home-screen app for the **paper** area: live status, today's trades, the report and screener, settings, controls
(pause, resume, emergency stop, restart) and push notifications. It runs on the VM next to the bot and is reachable
**only over your Tailscale network**: nothing new is open to the internet. Telegram keeps working as the backup.

The app can never place orders, switch to live, or read `.env` or broker credentials. How it is built and secured:
[APP_PLAN.md](APP_PLAN.md).

## 1. Tailscale account (once, about 5 minutes)

1. Create an account at tailscale.com, signing in with Google, Microsoft, GitHub or Apple. **Turn on 2-step
   verification or a passkey on that account**: whoever controls it can add devices to your network.
2. In the Tailscale admin console (login.tailscale.com/admin):
   - **DNS** page: turn on **MagicDNS** and **HTTPS Certificates**.
   - **Settings → Device management**: turn on **Device approval** (new devices need your OK).
   - **Settings → Tailnet Lock** (optional, strongest): turn it on, so new devices must also be signed by one of yours.
3. Access rules (**Access controls** page). This lets your own devices reach only the app on the VM:
   ```json
   {
     "tagOwners": { "tag:angelbot": ["autogroup:admin"] },
     "grants": [
       { "src": ["autogroup:member"], "dst": ["tag:angelbot"], "ip": ["tcp:8443"] }
     ]
   }
   ```
   Add `"tcp:22"` to `ip` if you also want to SSH to the VM over Tailscale. The admin console's editor and docs show the
   current format; adjust if Tailscale has changed it.

## 2. On the VM (once, about 5 minutes)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --advertise-tags=tag:angelbot       # opens a login link; approve the VM in the admin console
cd ~/angelbot && git pull
bash deploy/app_setup.sh
```

`app_setup.sh` checks Tailscale and MagicDNS, lets your user fetch certificates (`sudo tailscale set --operator`),
installs the Python packages, writes `runtime/paper/app/api.env` (address, name, port 8443), fetches the HTTPS
certificate, installs and starts:

| Unit | What it is |
|---|---|
| `angelbot-api@paper.service` | the app and its API, on `https://<vm>.<tailnet>.ts.net:8443` |
| `angelbot-tlscert@paper.timer` | renews the certificate daily at 06:10 (Tailscale certificates last 90 days) |

It prints the app's address at the end. Running it again is safe.

If this Ubuntu does not allow systemd's sandbox in user services (common on 24.04), the script says so and runs the API
with no-new-privileges and resource limits only (a drop-in in `~/.config/systemd/user/angelbot-api@paper.service.d/`).
The API's own checks (area, address, HTTPS, login) are unchanged.

## 3. On your phone (about 5 minutes)

1. Install the **Tailscale** app, log in with the same account, and approve the phone in the admin console.
2. On the VM: `./deploy/ab paper app enroll --name "My phone"`. It prints a code like `ABCDE-FGH23`, valid for 10
   minutes and usable once. (Telegram tells you a code was made.)
3. On the phone, with Tailscale on, open the address the setup printed:
   - **iPhone**: in **Safari**, then **Share → Add to Home Screen**, and open AngelBot from the home screen.
   - **Android**: in **Chrome**, then **⋮ → Install app** (or Add to Home screen).
4. In the app: type the code, tap **Register this device**, confirm with Face ID or fingerprint, then sign in.
5. **Settings → Turn on notifications**, then **Send a test**. On iPhone this needs iOS 16.4 or later and the
   home-screen app (not a Safari tab).

## 4. Using it

- **Home**: running or not, today's result after fees, the open position with its stop and target, what the strategy
  sees ("Buy signal if a minute closes above ₹…"), warnings, and the controls. Live over a WebSocket.
- **Actions** ask for Face ID if your last check is more than 2 minutes old. **Emergency stop** asks twice. **Restart**
  works only when the bot has no open position or waiting order (a paper position would be lost).
- **Trades** by day, **Screener** and **Report** (the same pages as Telegram), **Alerts**, **Settings** (the same
  choices as Telegram's ⚙️ Settings), **Devices** and **Sign out**.
- Sessions last 12 hours; then sign in again with Face ID.

## 5. Devices, lost phone, switching it off

```bash
./deploy/ab paper app devices                    # list registered devices
./deploy/ab paper app remove --id <id>           # remove one: it is signed out at once (Telegram is told)
./deploy/ab paper app signout-all                # sign every device out
systemctl --user stop angelbot-api@paper         # switch the app off; trading and Telegram carry on
systemctl --user disable --now angelbot-api@paper angelbot-tlscert@paper.timer   # off for good
```

**Lost or stolen phone**: remove it in the Tailscale admin console (it can no longer reach the VM), then
`app remove --id ...` on the VM. Nothing on the phone gives access to the broker.

## 6. Logs and checks

```bash
systemctl --user status angelbot-api@paper --no-pager
journalctl --user -u angelbot-api@paper -f
journalctl --user -u angelbot-tlscert@paper -n 50 --no-pager
tail -n 20 runtime/paper/app/audit.jsonl          # logins, actions, failures, lockouts
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| The page does not load on the phone | Tailscale is off on the phone, or the phone is not approved; check `tailscale status` on both |
| `app_setup.sh`: no MagicDNS name | turn on MagicDNS (admin console, DNS page) |
| `tailscale cert` failed | turn on HTTPS Certificates (DNS page); run `sudo tailscale set --operator=$USER` |
| "This browser has no passkey support" | use Safari (iPhone) or Chrome (Android) on a recent OS |
| Code refused | codes last 10 minutes and work once: make a new one with `app enroll` |
| "too many failed attempts" | 5 failures lock that device out for 15 minutes; Telegram tells you |
| Notifications never arrive on iPhone | open the app from the home screen (not Safari), iOS 16.4+, allow notifications in iOS Settings |
| "Can't reach the bot" | the VM or its Tailscale is down: `systemctl --user status angelbot-api@paper`, `tailscale status` |
| Certificate warning on Telegram | `systemctl --user start angelbot-tlscert@paper` and read its log |
