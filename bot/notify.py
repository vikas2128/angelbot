"""Alerts: written to the area's alert outbox (bot/alerts.py, for the app) and, if TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID are set, sent to Telegram.

Text is sent as Telegram HTML; build it with bot.messages (which escapes dynamic values).
"""
from __future__ import annotations

import html
import logging
import os
import re
import threading

import requests

log = logging.getLogger("angelbot.notify")

_mode: str | None = None
_replay = False
_muted = False


def set_mode(mode: str | None) -> None:
    """Called by the order manager so every alert is tagged PAPER or LIVE."""
    global _mode
    _mode = mode


def set_replay(flag: bool) -> None:
    """An offline replay: alerts are tagged TEST, never PAPER or LIVE."""
    global _replay
    _replay = flag


def set_muted(flag: bool) -> None:
    """While muted nothing is sent (a replay mutes the per-trade alerts and sends one summary at the end)."""
    global _muted
    _muted = flag


def label() -> str:
    """The area an alert comes from: 🔴 LIVE (real money), 📝 PAPER (real prices, simulated orders), 🧪 TEST (pytest, replay)."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return "\U0001F9EA <b>TEST</b> \u00b7 unit test"
    if _replay or os.environ.get("ANGELBOT_PROFILE") == "research":
        return "\U0001F9EA <b>TEST</b> \u00b7 replay"
    return "\U0001F534 <b>LIVE</b>" if _mode == "live" else "\U0001F4DD <b>PAPER</b>"


def plain(text: str) -> str:
    """Strip tags — used if Telegram rejects the HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send(text: str, buttons: list[list[dict]] | None = None) -> None:
    if _muted:
        return
    from bot import alerts
    alerts.append(text, plain(text), plain(label()))      # the app's outbox; never raises, never waits
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    text = f"{label()}\n{text}"

    def _go():
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": buttons}
        try:
            r = requests.post(url, json=body, timeout=10)
            if r.status_code == 400:               # bad HTML → resend as plain text, never lose an alert
                body.pop("parse_mode")
                body["text"] = plain(text)
                requests.post(url, json=body, timeout=10)
        except Exception as e:  # never let alerts break trading
            log.warning("telegram send failed: %s", e)

    threading.Thread(target=_go, daemon=True).start()
