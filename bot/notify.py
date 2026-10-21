"""Optional Telegram alerts. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env."""
from __future__ import annotations

import logging
import os
import threading

import requests

log = logging.getLogger("angelbot.notify")


def send(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return

    def _go():
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"[AngelBot] {text}"}, timeout=10)
        except Exception as e:  # never let alerts break trading
            log.warning("telegram send failed: %s", e)

    threading.Thread(target=_go, daemon=True).start()
