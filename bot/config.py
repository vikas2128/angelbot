"""Configuration: config/config.yaml (strategy, risk) + .env (secrets, mode)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("ANGELBOT_CONFIG", ROOT / "config" / "config.yaml"))
RUNTIME_DIR = Path(os.environ.get("ANGELBOT_RUNTIME", ROOT / "runtime"))
DATA_DIR = Path(os.environ.get("ANGELBOT_DATA", ROOT / "data"))

LIVE_CONFIRM_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader (no extra dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: Path | None = None) -> dict[str, Any]:
    with open(path or CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    """Raise ValueError with a readable message if the config is unsafe/invalid."""
    errs: list[str] = []
    for key in ("session", "instruments", "strategy", "exits", "risk", "execution"):
        if key not in cfg:
            errs.append(f"missing section: {key}")
    if errs:
        raise ValueError("; ".join(errs))
    if not cfg["instruments"]:
        errs.append("instruments list is empty")
    for i in cfg["instruments"]:
        for k in ("symbol", "exchange", "qty"):
            if k not in i:
                errs.append(f"instrument missing '{k}': {i}")
        if int(i.get("qty", 0)) <= 0:
            errs.append(f"qty must be > 0: {i}")
    r = cfg["risk"]
    if float(r["max_daily_loss"]) <= 0:
        errs.append("risk.max_daily_loss must be > 0")
    if not (0 < float(r["max_orders_per_sec"]) < 10):
        errs.append("risk.max_orders_per_sec must be between 0 and 10 (SEBI 10 OPS threshold)")
    if float(cfg["exits"]["stop_loss_pct"]) <= 0:
        errs.append("exits.stop_loss_pct must be > 0 (every trade needs a stop)")
    s = cfg["session"]
    if not (s["entry_start"] < s["entry_cutoff"] < s["squareoff"] < s["shutdown"]):
        errs.append("session times must satisfy entry_start < entry_cutoff < squareoff < shutdown")
    from bot.strategies import STRATEGIES  # local import to avoid cycle

    if cfg["strategy"]["name"] not in STRATEGIES:
        errs.append(f"unknown strategy {cfg['strategy']['name']!r}; options: {list(STRATEGIES)}")
    if errs:
        raise ValueError("; ".join(errs))


def trading_mode() -> str:
    """'paper' (default) or 'live'. Live needs BOTH env vars set — a deliberate two-key switch."""
    mode = os.environ.get("ANGELBOT_MODE", "paper").strip().lower()
    if mode == "live":
        if os.environ.get("ANGELBOT_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
            raise RuntimeError(
                "ANGELBOT_MODE=live but ANGELBOT_LIVE_CONFIRM is not set to the confirm phrase. "
                "Refusing to trade real money."
            )
        return "live"
    return "paper"


def credentials() -> dict[str, str]:
    keys = {
        "api_key": "ANGEL_API_KEY",
        "client_code": "ANGEL_CLIENT_CODE",
        "pin": "ANGEL_PIN",
        "totp_secret": "ANGEL_TOTP_SECRET",
    }
    out, missing = {}, []
    for k, env in keys.items():
        v = os.environ.get(env, "").strip()
        if not v:
            missing.append(env)
        out[k] = v
    if missing:
        raise RuntimeError(f"Missing credentials in .env: {', '.join(missing)}")
    return out
