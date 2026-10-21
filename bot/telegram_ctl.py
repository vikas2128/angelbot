"""Two-way Telegram control for AngelBot.

Runs as its own systemd service (angelbot-telegram.service) so a Telegram problem can
never affect the trading process. Talks to the bot only through runtime/control.json,
runtime/status.json and the journal — the same channels the CLI and Claude Code use.

Only messages from TELEGRAM_CHAT_ID are answered; everything else is ignored.
Deliberately NO order placement and NO paper→live switching.

    python -m bot.telegram_ctl
"""
from __future__ import annotations

import logging
import os
import sys
import time
from collections import deque
from datetime import datetime

import requests

from bot import config as C
from bot import control
from bot.journal import query_readonly
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.telegram")
API = "https://api.telegram.org/bot{token}/{method}"

HELP = """AngelBot commands
/status – alive?, mode, P&L, position, stop/target
/pnl – today's trades & net P&L
/pause – stop new entries (open positions stay managed)
/resume – allow entries again
/kill – flatten everything & stop (asks to confirm)
/log – last 20 log lines
/help – this list"""


# ───────────────────────────── command handlers ─────────────────────────────
def _fmt_money(x) -> str:
    try:
        return f"₹{float(x):,.2f}"
    except (TypeError, ValueError):
        return "–"


def cmd_status() -> str:
    s = control.read_status()
    c = control.read_control()
    try:
        mode = C.trading_mode()
    except RuntimeError as e:
        mode = f"INVALID ({e})"
    if not s.get("written_at"):
        return f"No status yet (bot hasn't run).\nmode configured: {mode}\ncontrol: {c}"
    alive = "🟢 running" if s.get("bot_alive") else f"⚪ not running (last update {s.get('age_sec', '?')}s ago)"
    lines = [
        f"{alive}  |  mode: {s.get('mode')}{' (replay)' if s.get('replay') else ''}",
        f"strategy: {s.get('strategy')}",
        f"day P&L: {_fmt_money(s.get('day_pnl'))}  (realised {_fmt_money(s.get('realized_net'))}, "
        f"charges {_fmt_money(s.get('charges_today'))})",
        f"trades today: {s.get('trades_today')}",
    ]
    for p in s.get("positions") or []:
        lines.append(f"📌 {p['symbol']} {p['qty']} @ {p['avg']}  ltp {p.get('ltp')}  "
                     f"uPnL {_fmt_money(p.get('unrealized'))}  SL {p.get('stop')}  TGT {p.get('target')}")
    if not s.get("positions"):
        lines.append("no open positions")
    if s.get("pending_orders"):
        lines.append(f"pending orders: {len(s['pending_orders'])}")
    if s.get("halted_reason"):
        lines.append(f"⛔ halted: {s['halted_reason']}")
    if s.get("cooldown_until"):
        lines.append(f"cooldown until {s['cooldown_until'][11:16]}")
    feed = s.get("feed") or {}
    if s.get("bot_alive") and not s.get("replay"):
        lines.append(f"feed: {'ok' if feed.get('connected') else 'DISCONNECTED'}  last tick "
                     f"{(feed.get('last_tick_at') or '–')[11:19]}  restarts {feed.get('restarts', 0)}")
    lines.append(f"control: paused={c['paused']} kill={c['kill']}")
    return "\n".join(lines)


def cmd_pnl() -> str:
    today = now_ist().strftime("%Y-%m-%d")
    rows = query_readonly(control.JOURNAL,
                          f"SELECT symbol, side, qty, entry_price, exit_price, net_pnl, exit_reason, exit_ts "
                          f"FROM trades WHERE substr(exit_ts,1,10)='{today}' "
                          f"ORDER BY exit_ts", 50)
    if not rows:
        return f"No closed trades today ({today})."
    net = sum(r["net_pnl"] or 0 for r in rows)
    wins = sum(1 for r in rows if (r["net_pnl"] or 0) > 0)
    out = [f"Trades {today}: {len(rows)}  wins {wins}  net {_fmt_money(net)}"]
    for r in rows[-15:]:
        out.append(f"{r['exit_ts'][11:16]} {r['side'][0]} {r['symbol']} {r['entry_price']}→{r['exit_price']} "
                   f"{_fmt_money(r['net_pnl'])} ({r['exit_reason']})")
    return "\n".join(out)


def cmd_log(n: int = 20) -> str:
    if not control.LOG.exists():
        return "(no log yet)"
    dq: deque[str] = deque(maxlen=n)
    with open(control.LOG, errors="replace") as f:
        for line in f:
            dq.append(line.rstrip()[:200])
    return "\n".join(dq) or "(log empty)"


def handle(text: str) -> str:
    """Pure command router (unit-testable). Returns the reply text."""
    parts = (text or "").strip().split()
    if not parts:
        return HELP
    cmd = parts[0].split("@")[0].lower()        # strip /cmd@botname
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return cmd_status()
    if cmd == "/pnl":
        return cmd_pnl()
    if cmd == "/log":
        return cmd_log()
    if cmd == "/pause":
        control.set_control(paused=True, note="telegram pause")
        return "⏸ Paused: no new entries. Open positions keep their stop/target/square-off."
    if cmd == "/resume":
        control.set_control(paused=False, kill=False, note="telegram resume")
        return "▶️ Resumed: entries allowed."
    if cmd == "/kill":
        if arg != "CONFIRM":
            return ("⚠️ This cancels entries, EXITS ALL positions with limit orders and stops the bot.\n"
                    "Send  /kill CONFIRM  to proceed.")
        control.set_control(kill=True, note="telegram kill")
        return ("⛔ Kill switch set. Bot flattens within ~1s if running, then stops (left paused).\n"
                "Verify with /status and in the Angel One app. Use /resume before the next session.")
    return "Unknown command.\n\n" + HELP


# ───────────────────────────── Telegram plumbing ─────────────────────────────
class TelegramController:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, str(chat_id)
        self.offset: int | None = None
        self.s = requests.Session()

    def _api(self, method: str, **params):
        r = self.s.post(API.format(token=self.token, method=method), json=params, timeout=70)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data)
        return data["result"]

    def send(self, text: str) -> None:
        for i in range(0, len(text), 3900):            # Telegram limit is 4096 chars
            self._api("sendMessage", chat_id=self.chat_id, text=text[i:i + 3900])

    def poll_once(self) -> int:
        params = {"timeout": 50, "allowed_updates": ["message"]}
        if self.offset is not None:
            params["offset"] = self.offset
        updates = self._api("getUpdates", **params)
        for u in updates:
            self.offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if chat != self.chat_id:
                log.warning("ignored message from unauthorised chat %s", chat)
                continue
            log.info("command: %s", text[:50])
            try:
                reply = handle(text)
            except Exception as e:                      # never die on a bad command
                log.exception("handler error")
                reply = f"Error: {e}"
            self.send(reply)
        return len(updates)

    def skip_backlog(self) -> None:
        """Ignore commands sent while the service was down (e.g. a stale /kill)."""
        updates = self._api("getUpdates", timeout=0)
        if updates:
            self.offset = updates[-1]["update_id"] + 1
            log.info("skipped %d old update(s)", len(updates))

    def run(self) -> None:
        self.skip_backlog()
        self.send("🤖 AngelBot Telegram control online. /help for commands.")
        backoff = 1
        while True:
            try:
                self.poll_once()
                backoff = 1
            except Exception as e:
                log.warning("poll error: %s (retry in %ss)", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


def main() -> None:
    C.load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env — nothing to do")
        sys.exit(0)
    TelegramController(token, chat).run()


if __name__ == "__main__":
    main()
