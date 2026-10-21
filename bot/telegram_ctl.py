"""Two-way Telegram control for AngelBot.

Runs as its own systemd service (angelbot-telegram.service) so a Telegram problem can
never affect the trading process. Talks to the bot only through runtime/control.json,
runtime/status.json and the journal — the same channels the CLI and Claude Code use.

Only messages from TELEGRAM_CHAT_ID are answered; everything else is ignored.
Deliberately NO order placement and NO paper→live switching.

    python -m bot.telegram_ctl
"""
from __future__ import annotations

import html
import importlib.util
import logging
import os
import re
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import requests

from bot import config as C
from bot import control, notify
from bot import messages as M
from bot.journal import query_readonly
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.telegram")
API = "https://api.telegram.org/bot{token}/{method}"

HELP = (
    "<b>AngelBot commands</b>\n"
    "/status - is it running, today's result, open position\n"
    "/pnl - today's trades and result (also /summary)\n"
    "/pause - stop new trades (open ones stay protected)\n"
    "/resume - allow new trades again\n"
    "/kill - close everything and stop (asks to confirm)\n"
    "/report - send the performance report as a file (optional: all|session|replay and a start date YYYY-MM-DD)\n"
    "/log - last 20 log lines\n"
    "/menu - show buttons\n"
    "/help - this list"
)

KILL_TTL_SEC = 60          # a "Yes, stop everything" button works for one minute only

COMMANDS = [("status", "Status and open position"), ("pnl", "Today's trades and result"),
            ("pause", "Stop new trades"), ("resume", "Allow new trades"),
            ("report", "Send the performance report"),
            ("kill", "Emergency stop"), ("log", "Recent log lines"), ("menu", "Show buttons")]

REPORT_SKILL = C.ROOT / "skills" / "angelbot-report" / "build_report.py"


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def main_menu() -> list[list[dict]]:
    return [[_btn("📊 Status", "status"), _btn("💰 Today", "summary")],
            [_btn("⏸ Pause", "pause"), _btn("▶️ Resume", "resume")],
            [_btn("📄 Report", "report"), _btn("🛑 Emergency stop", "kill_ask")]]


def _refresh_row() -> list[list[dict]]:
    return [[_btn("🔄 Refresh", "status"), _btn("☰ Menu", "menu")]]


# ───────────────────────────── command handlers ─────────────────────────────
def cmd_status() -> str:
    s = control.read_status()
    c = control.read_control()
    try:
        mode = C.trading_mode()
    except RuntimeError as e:
        mode = f"INVALID ({e})"
    return M.status_card(s, c, mode)


def cmd_pnl() -> str:
    today = now_ist().strftime("%Y-%m-%d")
    rows = query_readonly(control.JOURNAL,
                          "SELECT mode, symbol, side, qty, entry_price, exit_price, gross_pnl, charges, "
                          f"net_pnl, exit_reason, exit_ts FROM trades WHERE substr(exit_ts,1,10)='{today}' "
                          "ORDER BY exit_ts", 200)
    mode = rows[0]["mode"] if rows else None
    return M.daily_summary(rows, today, mode)


def cmd_log(n: int = 20) -> str:
    if not control.LOG.exists():
        return "(no log yet)"
    dq: deque[str] = deque(maxlen=n)
    with open(control.LOG, errors="replace") as f:
        for line in f:
            dq.append(line.rstrip()[:160])
    body = "\n".join(dq) or "(log empty)"
    return "<pre>" + html.escape(body[-3500:]) + "</pre>"      # one message, tags always balanced


def _load_report_builder():
    spec = importlib.util.spec_from_file_location("angelbot_build_report", REPORT_SKILL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_report_file(arg: str, out_dir: Path) -> tuple[Path, str]:
    """Build the standalone HTML report from the journal (read-only). Returns (file, caption HTML).

    Raises ValueError with a message that is safe to show the user when there is nothing to report.
    """
    source, since = "all", "0000-00-00"
    for word in (arg or "").split():
        if word in ("all", "session", "replay"):
            source = word
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", word):
            since = word
        else:
            raise ValueError(f"Unknown option '{word}'. Use /report [all|session|replay] [YYYY-MM-DD].")
    if not control.JOURNAL.exists():
        raise ValueError("No journal yet: the bot has not recorded any trades.")
    if not REPORT_SKILL.exists():
        raise ValueError("Report builder not found on this machine (skills/angelbot-report).")
    br = _load_report_builder()
    try:
        rows = br.load_rows(SimpleNamespace(db=str(control.JOURNAL), json=None, since=since,
                                            until="9999-99-99", source=source))
    except SystemExit as e:                        # the builder exits with a message on bad input
        raise ValueError(str(e)) from None
    if not rows:
        raise ValueError("No closed trades match that filter yet.")
    try:
        timeframe = f"{C.load_config()['strategy'].get('timeframe_min', 1)}-minute"
    except Exception:
        timeframe = "1-minute"
    st = control.read_status()
    setup = [{"item": "Bot process", "state": "ok" if st.get("bot_alive") else "off",
              "detail": "Running" if st.get("bot_alive") else "Not running right now"},
             {"item": "Trading mode", "state": "warn" if st.get("mode") == "live" else "ok",
              "detail": str(st.get("mode") or "unknown")}] if st.get("written_at") else None
    data = br.build(rows, timeframe, setup)
    out = out_dir / f"angelbot-report-{now_ist():%Y-%m-%d}.html"
    br.render(data, out)
    k, m = data["kpis"], data["meta"]
    pf = "n/a" if k["profit_factor"] is None else f"{k['profit_factor']:.2f}"
    notes = []
    if m["basis"] != "session":
        notes.append("Includes replayed candles, not only live sessions." if m["basis"] == "mixed"
                     else "Replayed candles, not a live session.")
    if k["trades"] < 100:
        notes.append("Under 100 trades: too few to judge a strategy.")
    caption = (f"📄 <b>AngelBot report</b> · {k['trades']} trade{'' if k['trades'] == 1 else 's'}"
               f" · {m['first_day']} to {m['last_day']}\n"
               f"{M.mode_tag('live' if 'live' in m['mode'] else 'paper')}\n"
               f"Net <b>{M.money(k['net'])}</b> after fees · win rate {k['win_rate'] * 100:.1f}% · "
               f"profit factor {pf} · max drawdown {M.money(k['max_dd'])}\n"
               + " ".join(notes) + "\nOpen the file in your phone's browser.")
    return out, caption.strip()


def kill_ask() -> tuple[str, list[list[dict]]]:
    text = ("⚠️ <b>Emergency stop</b>\nThis cancels waiting entries, closes ALL positions and stops the bot.\n"
            "Tap the button below within a minute, or send /kill CONFIRM.")
    return text, [[_btn("🛑 Yes, stop everything", f"kill_do:{int(time.time())}")],
                  [_btn("Cancel", "menu")]]


def kill_do() -> str:
    control.set_control(kill=True, note="telegram kill")
    return ("⛔ <b>Kill switch set</b>\nThe bot closes positions within about a second if it is running, "
            "then stops (left paused).\nCheck /status and the Angel One app. Use /resume before the next session.")


def route(text: str) -> tuple[str, list[list[dict]] | None]:
    """Pure command router (unit-testable). Returns (reply HTML, optional inline buttons)."""
    parts = (text or "").strip().split()
    if not parts:
        return HELP, main_menu()
    cmd = parts[0].split("@")[0].lower()        # strip /cmd@botname
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help", "/menu"):
        return HELP, main_menu()
    if cmd == "/status":
        return cmd_status(), _refresh_row()
    if cmd in ("/pnl", "/summary"):
        return cmd_pnl(), main_menu()
    if cmd == "/log":
        return cmd_log(), None
    if cmd == "/report":                        # the controller sends the file; this is only a fallback text
        return "📄 Building the report…", None
    if cmd == "/pause":
        control.set_control(paused=True, note="telegram pause")
        return "⏸ <b>Paused</b>: no new trades. Open positions keep their safety exit, goal and market-close exit.", main_menu()
    if cmd == "/resume":
        control.set_control(paused=False, kill=False, note="telegram resume")
        return "▶️ <b>Resumed</b>: new trades allowed.", main_menu()
    if cmd == "/kill":
        if arg != "CONFIRM":
            return kill_ask()
        return kill_do(), None
    return "Unknown command.\n\n" + HELP, main_menu()


def handle(text: str) -> str:
    return route(text)[0]


def callback(data: str, now: float | None = None) -> tuple[str, list[list[dict]] | None]:
    """Route an inline-button press to the same actions as the typed commands."""
    action, _, arg = (data or "").partition(":")
    if action == "status":
        return cmd_status(), _refresh_row()
    if action == "summary":
        return cmd_pnl(), main_menu()
    if action == "pause":
        return route("/pause")
    if action == "resume":
        return route("/resume")
    if action == "menu":
        return HELP, main_menu()
    if action == "kill_ask":
        return kill_ask()
    if action == "kill_do":
        try:
            fresh = (now or time.time()) - int(arg) <= KILL_TTL_SEC
        except ValueError:
            fresh = False
        if not fresh:
            return "That confirmation expired. Ask again with /kill.", main_menu()
        return kill_do(), None
    return "Unknown button.", main_menu()


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

    def send(self, text: str, markup=None) -> None:
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        for n, chunk in enumerate(chunks):
            params = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True}
            if markup and n == len(chunks) - 1:
                params["reply_markup"] = {"inline_keyboard": markup}
            try:
                self._api("sendMessage", **params)
            except Exception:                           # bad HTML → resend as plain text
                params.pop("parse_mode")
                params["text"] = notify.plain(chunk)
                self._api("sendMessage", **params)

    def send_document(self, path: Path, caption: str = "") -> None:
        url = API.format(token=self.token, method="sendDocument")
        for parse_mode in ("HTML", None):
            data = {"chat_id": self.chat_id, "caption": (caption if parse_mode else notify.plain(caption))[:1000]}
            if parse_mode:
                data["parse_mode"] = parse_mode
            with open(path, "rb") as f:
                r = self.s.post(url, data=data, files={"document": (Path(path).name, f, "text/html")}, timeout=70)
            if r.status_code == 400 and parse_mode:         # bad caption HTML → resend as plain text
                continue
            r.raise_for_status()
            if not r.json().get("ok"):
                raise RuntimeError(r.json())
            return

    def send_report(self, arg: str = "") -> None:
        """Build the report from the journal and send it as a file. Read-only; never touches the bot."""
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path, caption = build_report_file(arg, Path(tmp))
                self.send_document(path, caption)
        except ValueError as e:
            self.send("📄 " + html.escape(str(e)))
        except Exception as e:                              # never die on a report problem
            log.exception("report error")
            self.send("📄 Couldn't build the report: " + html.escape(str(e)))

    def edit(self, message_id: int, text: str, markup=None) -> None:
        """Update the message the button was on, so the chat stays tidy."""
        params = {"chat_id": self.chat_id, "message_id": message_id, "text": text[:3900],
                  "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            params["reply_markup"] = {"inline_keyboard": markup}
        try:
            self._api("editMessageText", **params)
        except Exception as e:
            if "not modified" in str(e):
                return
            log.info("edit failed (%s), sending new message", e)
            self.send(text, markup)

    def _handle_callback(self, cq: dict) -> None:
        msg = cq.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        try:
            self._api("answerCallbackQuery", callback_query_id=cq["id"])
        except Exception as e:
            log.warning("answerCallbackQuery failed: %s", e)
        if chat != self.chat_id:
            log.warning("ignored button press from unauthorised chat %s", chat)
            return
        data = cq.get("data") or ""
        log.info("button: %s", data.split(":")[0])
        if data == "report":                                # a file can't replace the message: send it as a new one
            self.send_report()
            return
        try:
            text, markup = callback(data)
        except Exception as e:
            log.exception("callback error")
            text, markup = f"Error: {html.escape(str(e))}", None
        self.edit(msg.get("message_id"), text, markup)

    def poll_once(self) -> int:
        params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
        if self.offset is not None:
            params["offset"] = self.offset
        updates = self._api("getUpdates", **params)
        for u in updates:
            self.offset = u["update_id"] + 1
            if u.get("callback_query"):
                self._handle_callback(u["callback_query"])
                continue
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if chat != self.chat_id:
                log.warning("ignored message from unauthorised chat %s", chat)
                continue
            log.info("command: %s", text[:50])
            words = text.strip().split()
            if words and words[0].split("@")[0].lower() == "/report":
                self.send_report(" ".join(words[1:]))
                continue
            try:
                reply, markup = route(text)
            except Exception as e:                      # never die on a bad command
                log.exception("handler error")
                reply, markup = f"Error: {html.escape(str(e))}", None
            self.send(reply, markup)
        return len(updates)

    def skip_backlog(self) -> None:
        """Ignore commands and button taps made while the service was down (e.g. a stale /kill)."""
        updates = self._api("getUpdates", timeout=0)
        if updates:
            self.offset = updates[-1]["update_id"] + 1
            log.info("skipped %d old update(s)", len(updates))

    def run(self) -> None:
        self.skip_backlog()
        try:
            self._api("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except Exception as e:
            log.warning("setMyCommands failed: %s", e)
        self.send("🤖 <b>AngelBot control is online</b>\nTap a button or send /help.", main_menu())
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
