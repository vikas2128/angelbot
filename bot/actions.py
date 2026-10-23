"""The actions a person can take on a running area, shared by every channel (Telegram, the app's API).

One place for the rules, so the channels can never behave differently: pause, resume, emergency stop, restarting the
paper bot, and changing settings. Each action records which channel asked (`source`) in control.json's note.
Deliberately NO order placement and NO paper -> live switch: those do not exist here, by design.
"""
from __future__ import annotations

import logging
import os
import subprocess

from bot import control
from bot import settings as ST
from bot.timeutil import now_ist

log = logging.getLogger("angelbot.actions")

RESTART_CMD = ["systemctl", "--user", "--no-block", "restart", "angelbot@paper.service"]


def pause(source: str) -> dict:
    """No new entries; open positions keep their stop, target and market-close exit."""
    return control.set_control(paused=True, note=f"{source} pause")


def resume(source: str) -> dict:
    """Allow new entries again; also clears an emergency stop."""
    return control.set_control(paused=False, kill=False, note=f"{source} resume")


def kill(source: str) -> dict:
    """Emergency stop: the bot cancels waiting entries, exits everything with limit orders, and stops (left paused).
    Channels must ask for a confirmation before calling this."""
    log.warning("emergency stop requested from %s", source)
    return control.set_control(kill=True, note=f"{source} kill")


def restart_refusal() -> str | None:
    """Why the bot cannot be restarted remotely right now (None: it can)."""
    if os.environ.get("ANGELBOT_PROFILE") != "paper":
        return "Restarting remotely is only for the paper area. Restart this one on the VM yourself."
    s = control.read_status()
    if not s.get("bot_alive") or s.get("replay"):
        return "The bot isn't running, so there is nothing to restart: the changes apply at its next start."
    if s.get("positions") or s.get("pending_orders"):
        return ("The bot has an open position or a waiting order. A paper position lives only in the bot's memory "
                "and a restart would lose it, so restart after it closes.")
    return None


def restart(source: str, runner=subprocess.run) -> tuple[bool, str]:
    """Restart the paper bot if allowed. Returns (done, why not / error). Checks the refusal again first."""
    why = restart_refusal()
    if why:
        return False, why
    try:
        r = runner(RESTART_CMD, capture_output=True, text=True, timeout=30)
        ok, err = r.returncode == 0, (r.stderr or "").strip()
    except Exception as e:
        ok, err = False, str(e)
    if not ok:
        log.warning("restart from %s failed: %s", source, err)
        return False, err
    log.warning("bot restart requested from %s", source)
    return True, ""


def set_setting(sid: str, index: int | None = None, value=None) -> tuple[str, str]:
    """Set one setting to one of its choices (index) or, for a typed setting, a value. Returns (old, new) as shown
    to people. Raises settings.SettingsError (a ValueError) with a readable message; nothing is saved then."""
    if value is not None:
        return ST.apply_value(sid, value, now=now_ist())
    return ST.apply(sid, -1 if index is None else int(index), now=now_ist())


def reset_settings() -> int:
    """Drop every override back to config.yaml. Returns how many there were."""
    return ST.reset(now=now_ist())
