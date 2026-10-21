"""Optional Telegram alerts. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.

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


def plain(text: str) -> str:
    """Strip tags — used if Telegram rejects the HTML."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send(text: str, buttons: list[list[dict]] | None = None) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return

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
