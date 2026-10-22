"""File-based control plane shared by the bot, the CLI and the MCP server.

runtime/control.json  ← written by CLI/MCP  {paused, kill, note, updated}
runtime/status.json   ← written by the bot every couple of seconds
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from bot.config import RUNTIME_DIR
from bot.timeutil import now_ist

CONTROL = RUNTIME_DIR / "control.json"
STATUS = RUNTIME_DIR / "status.json"
JOURNAL = RUNTIME_DIR / "journal.db"
SCREEN = RUNTIME_DIR / "screen.json"
SEL_REQUEST = RUNTIME_DIR / "selection_request.json"   # bot -> Telegram: today's picks and whether it is still waiting
SEL_CHOICE = RUNTIME_DIR / "selection_choice.json"     # Telegram -> bot: what you tapped
SEL_STATE = RUNTIME_DIR / "selection_state.json"       # what the bot decided today (reused after a restart)
LOG = RUNTIME_DIR / "bot.log"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.chmod(tmp, 0o644)        # mkstemp makes 0600; let a read-only analyst user read status.json
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def read_control() -> dict:
    c = read_json(CONTROL)
    return {"paused": bool(c.get("paused")), "kill": bool(c.get("kill")), "note": c.get("note", "")}


def set_control(paused: bool | None = None, kill: bool | None = None, note: str = "") -> dict:
    c = read_control()
    if paused is not None:
        c["paused"] = paused
    if kill is not None:
        c["kill"] = kill
    c["note"] = note
    c["updated"] = now_ist().isoformat()
    _atomic_write(CONTROL, c)
    return c


def write_status(status: dict) -> None:
    status["written_at"] = now_ist().isoformat()
    _atomic_write(STATUS, status)


def write_json(path: Path, data: dict) -> None:
    _atomic_write(path, data)


def write_screen(snapshot: dict) -> None:
    _atomic_write(SCREEN, snapshot)


def read_screen() -> dict:
    return read_json(SCREEN)


def read_status() -> dict:
    s = read_json(STATUS)
    if s.get("written_at"):
        age = (now_ist() - datetime.fromisoformat(s["written_at"])).total_seconds()
        s["age_sec"] = round(age, 1)
        s["bot_alive"] = age < 15 and s.get("state") not in ("stopped",)
    else:
        s["bot_alive"] = False
    return s
