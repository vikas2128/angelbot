"""Alert outbox: every alert the bot sends (the same text Telegram gets) is also appended to
runtime/<area>/alerts.jsonl, one JSON object per line, for the app's API to show and push.

The bot never waits on anyone reading it and never fails because of it: a write error is logged and ignored. The
file is kept small: past MAX_BYTES it is cut down to the newest KEEP_LINES alerts.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from bot import config as C
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.alerts")

MAX_BYTES = 512 * 1024
KEEP_LINES = 1000
_lock = threading.Lock()


def path() -> Path:
    return C.RUNTIME_DIR / "alerts.jsonl"


def append(text_html: str, text_plain: str, label: str) -> None:
    """Record one alert. Never raises."""
    rec = {"ts": now_ist().isoformat(timespec="seconds"), "area": os.environ.get("ANGELBOT_PROFILE") or "",
           "label": label, "html": text_html, "text": text_plain}
    try:
        with _lock:
            p = path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if p.stat().st_size > MAX_BYTES:
                _trim(p)
    except Exception as e:                            # alerts must never break trading
        log.warning("alert outbox write failed: %s", e)


def _trim(p: Path) -> None:
    lines = p.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, p)


def read(since: str | None = None, limit: int = 50) -> list[dict]:
    """The newest alerts (oldest first), optionally only those after the ISO time `since`. Bad lines are skipped."""
    try:
        lines = path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and str(rec.get("ts", "")) <= since:
            continue
        out.append(rec)
    return out[-max(1, min(int(limit), KEEP_LINES)):]
