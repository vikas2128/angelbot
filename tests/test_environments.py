"""Paper / live / research / test are separate areas: their own files, services, Telegram bots and labels."""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bot import cli, config as C, control, messages, notify
from bot.journal import Journal, query_readonly
from conftest import make_bars

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_MASTER = Path(__file__).parent / "fixtures" / "scrip_sample.json"


def parse_env(path: Path) -> dict:
    """Parse a profile file the way systemd's EnvironmentFile does: KEY=value, whole-line comments only."""
    out = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        assert "=" in line, f"{path.name}:{n} is not KEY=value: {line!r}"
        k, v = line.split("=", 1)
        assert " #" not in v, f"{path.name}:{n} has a trailing comment (systemd would keep it in the value)"
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- labels and muting
def test_labels_by_area(monkeypatch):
    monkeypatch.setattr(notify, "_replay", False)
    monkeypatch.setattr(notify, "_mode", "live")
    assert "TEST" in notify.label() and "unit test" in notify.label()           # pytest can never look LIVE
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert "LIVE" in notify.label()
    monkeypatch.setattr(notify, "_mode", "paper")
    assert "PAPER" in notify.label() and "LIVE" not in notify.label()
    monkeypatch.setattr(notify, "_replay", True)
    assert "TEST" in notify.label() and "replay" in notify.label() and "PAPER" not in notify.label()
    monkeypatch.setattr(notify, "_replay", False)
    monkeypatch.setenv("ANGELBOT_PROFILE", "research")
    assert "replay" in notify.label()


def test_muted_sends_nothing(monkeypatch):
    posts = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: posts.append(k) or type("R", (), {"status_code": 200})())
    monkeypatch.setattr(notify.threading, "Thread", lambda target, daemon: type("T", (), {"start": staticmethod(target)})())
    monkeypatch.setattr(notify, "_muted", True)
    notify.send("hello")
    assert posts == []
    monkeypatch.setattr(notify, "_muted", False)
    notify.send("hello")
    assert len(posts) == 1


def test_mode_tag_words():
    assert "PAPER" in messages.mode_tag("paper") and "real prices, simulated orders" in messages.mode_tag("paper")
    assert "LIVE" in messages.mode_tag("live") and "real money" in messages.mode_tag("live")


# ---------------------------------------------------------------- tests are isolated from the bots
def test_the_test_run_cannot_reach_the_paper_or_live_bot():
    assert os.environ.get("ANGELBOT_PROFILE") is None and os.environ.get("ANGELBOT_CONFIG") is None
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    assert token is None or token == os.environ.get("ANGELBOT_TEST_TELEGRAM_BOT_TOKEN")   # only the Test bot, if set at all
    assert "angelbot_test_" in str(control.RUNTIME_DIR)                                   # a temp dir, never runtime/paper


# ---------------------------------------------------------------- one area, one set of files
@pytest.mark.parametrize("area", ["paper", "live", "research"])
def test_runtime_paths_follow_the_area(tmp_path, area):
    env = {**os.environ, "ANGELBOT_RUNTIME": str(tmp_path / area), "PYTHONPATH": str(ROOT)}
    out = subprocess.run([sys.executable, "-c", "from bot import control as c; print(c.JOURNAL, c.STATUS, c.CONTROL, c.SCREEN, c.SEL_STATE)"],
                         env=env, cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    assert len(out) == 5 and all(p.startswith(str(tmp_path / area)) for p in out)


def test_the_paper_area_pins_paper_even_if_the_shared_env_says_live(tmp_path, monkeypatch):
    shared = tmp_path / ".env"
    shared.write_text("ANGELBOT_MODE=live\nANGELBOT_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK\n")
    monkeypatch.setenv("ANGELBOT_MODE", parse_env(ROOT / "profiles" / "paper.env.example")["ANGELBOT_MODE"])   # what systemd would set
    monkeypatch.delenv("ANGELBOT_LIVE_CONFIRM", raising=False)
    C.load_dotenv(shared)
    assert C.trading_mode() == "paper"


def test_profile_templates_are_consistent_and_hold_no_secrets():
    envs = {a: parse_env(ROOT / "profiles" / f"{a}.env.example") for a in ("paper", "live", "research")}
    assert {a: e["ANGELBOT_PROFILE"] for a, e in envs.items()} == {"paper": "paper", "live": "live", "research": "research"}
    runtimes = [e["ANGELBOT_RUNTIME"] for e in envs.values()]
    assert len(set(runtimes)) == 3 and all(r.startswith("/") and r.rstrip("/").endswith(a) for r, a in zip(runtimes, envs))
    assert envs["paper"]["ANGELBOT_MODE"] == "paper" and envs["research"]["ANGELBOT_MODE"] == "paper"
    assert "ANGELBOT_MODE" not in envs["live"] and "ANGELBOT_LIVE_CONFIRM" not in envs["live"]    # live is a human-only switch
    assert envs["live"]["ANGELBOT_CONFIG"].endswith("config.live.yaml")
    for e in envs.values():
        assert e["TELEGRAM_BOT_TOKEN"] == "" and e["TELEGRAM_CHAT_ID"] == ""                      # templates carry no secrets
        assert not any(k.startswith("ANGEL_") for k in e)                                          # broker credentials stay in .env
    live_text = (ROOT / "profiles" / "live.env.example").read_text()
    assert "# ANGELBOT_MODE=live" in live_text and "# ANGELBOT_LIVE_CONFIRM=" in live_text           # present, but commented out


def test_systemd_templates_fail_closed_and_keep_live_off():
    svc = (ROOT / "deploy" / "angelbot@.service").read_text()
    assert "EnvironmentFile=%h/angelbot/profiles/%i.env" in svc and "EnvironmentFile=-" not in svc    # no file, no start
    assert "RestartPreventExitStatus=2" in svc and "RuntimeMaxSec=8h" in svc
    tg = (ROOT / "deploy" / "angelbot-telegram@.service").read_text()
    assert "EnvironmentFile=%h/angelbot/profiles/%i.env" in tg and "bot.telegram_ctl" in tg
    assert "Unit=angelbot@%i.service" in (ROOT / "deploy" / "angelbot@.timer").read_text()
    setup = (ROOT / "deploy" / "setup_oci.sh").read_text()
    enabled = re.findall(r"systemctl --user (?:enable|start)[^\n]*", setup)
    assert not any("@live" in line for line in enabled)                                              # the setup script never enables live


# ---------------------------------------------------------------- the wrapper
def make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "deploy").mkdir(parents=True)
    (proj / "profiles").mkdir()
    (proj / ".venv" / "bin").mkdir(parents=True)
    (proj / "deploy" / "ab").write_text((ROOT / "deploy" / "ab").read_text())
    (proj / "deploy" / "ab").chmod(0o755)
    py = proj / ".venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho "$ANGELBOT_PROFILE|$ANGELBOT_MODE|$ANGELBOT_RUNTIME|$TELEGRAM_BOT_TOKEN|$*"\n')
    py.chmod(0o755)
    return proj


def run_ab(proj: Path, *args):
    return subprocess.run(["bash", str(proj / "deploy" / "ab"), *args], capture_output=True, text=True, cwd=proj)


def test_wrapper_runs_a_command_inside_one_area(tmp_path):
    proj = make_project(tmp_path)
    rt = tmp_path / "rt" / "research"
    (proj / "profiles" / "research.env").write_text(
        f"# comment\nANGELBOT_PROFILE=research\nANGELBOT_MODE=paper\nANGELBOT_RUNTIME={rt}\nTELEGRAM_BOT_TOKEN=testbot\n")
    r = run_ab(proj, "research", "status")
    assert r.returncode == 0 and r.stdout.strip() == f"research|paper|{rt}|testbot|-m bot.cli status"
    assert rt.is_dir()                                                      # the area's folder is created


def test_wrapper_refuses_unknown_or_unconfigured_areas(tmp_path):
    proj = make_project(tmp_path)
    assert run_ab(proj).returncode == 2
    r = run_ab(proj, "staging", "status")
    assert r.returncode == 2 and "unknown area" in r.stderr
    r = run_ab(proj, "live", "status")                                      # no profiles/live.env: live stays unreachable
    assert r.returncode == 2 and "missing profiles/live.env" in r.stderr


# ---------------------------------------------------------------- bare control commands
def test_a_bare_kill_cannot_miss_the_running_bot(tmp_path, monkeypatch):
    monkeypatch.delenv("ANGELBOT_RUNTIME", raising=False)
    monkeypatch.delenv("ANGELBOT_PROFILE", raising=False)
    monkeypatch.setattr(C, "ROOT", tmp_path)
    cli._no_area_guard("kill")                                              # before the areas exist: legacy behaviour
    (tmp_path / "runtime" / "paper").mkdir(parents=True)
    with pytest.raises(SystemExit, match="./deploy/ab paper kill"):
        cli._no_area_guard("kill")
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    cli._no_area_guard("kill")                                              # with an area given it goes through


# ---------------------------------------------------------------- replays
def replay_args(tmp_path):
    rows = [[b.start.isoformat(), b.open, b.high, b.low, b.close, 1000] for b in make_bars([800 + 0.01 * k for k in range(375)])]
    csv_path = tmp_path / "SBIN-EQ_ONE_MINUTE.csv"
    csv_path.write_text("timestamp,open,high,low,close,volume\n" + "\n".join(",".join(map(str, r)) for r in rows))
    return argparse.Namespace(replay=[str(csv_path)], master=str(FIXTURE_MASTER), journal=str(tmp_path / "replay.db"))


def run_replay(tmp_path, cfg, monkeypatch, profile):
    sent = []
    for flag in ("_replay", "_muted"):
        monkeypatch.setattr(notify, flag, False)                            # restored after the test
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    if profile:
        monkeypatch.setenv("ANGELBOT_PROFILE", profile)
    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: sent.append(json) or type("R", (), {"status_code": 200})())
    monkeypatch.setattr(notify.threading, "Thread", lambda target, daemon: type("T", (), {"start": staticmethod(target)})())
    cli.cmd_run(replay_args(tmp_path), cfg)
    return sent


def test_a_replay_sends_one_summary_to_the_test_bot_only(tmp_path, cfg, monkeypatch):
    sent = run_replay(tmp_path, cfg, monkeypatch, "research")
    assert len(sent) == 1                                                    # one summary, not one message per trade
    assert "Replay finished" in sent[0]["text"] and "TEST" in sent[0]["text"] and "replay" in sent[0]["text"]
    ev = query_readonly(tmp_path / "replay.db", "SELECT msg FROM events WHERE kind='start'")
    assert ev and all("replay=True" in e["msg"] and "profile=research" in e["msg"] for e in ev)


def test_a_replay_outside_the_research_area_sends_nothing(tmp_path, cfg, monkeypatch):
    assert run_replay(tmp_path, cfg, monkeypatch, None) == []               # e.g. launched with the paper bot's token in the shell
    assert run_replay(tmp_path, cfg, monkeypatch, "paper") == []


# ---------------------------------------------------------------- secrets and the MCP server
def test_profile_secrets_are_protected_but_the_templates_stay_readable():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]
    deny = settings["deny"]
    for rule in ("Read(./profiles/*.env)", "Edit(./profiles/*.env)", "Write(./profiles/*.env)", "Bash(cat profiles/*.env:*)"):
        assert rule in deny
    assert "Read(./.env)" in deny                                              # the original rules are still there
    assert not any(r.startswith("Read(") and r.endswith(".example)") for r in deny)
    assert not any("live" in a for a in settings["allow"] if "angelbot" in a and "@" in a)
    assert "profiles/*.env" in (ROOT / ".gitignore").read_text().splitlines()


def mcp_runtime(extra_env: dict) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANGELBOT_", "TELEGRAM_"))}
    env.update(extra_env, PYTHONPATH=str(ROOT))
    code = "import mcp_server.server, bot.control as c; print(c.RUNTIME_DIR)"
    return subprocess.run([sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def test_the_mcp_server_follows_its_area():
    assert mcp_runtime({"ANGELBOT_PROFILE": "paper"}) == str(ROOT / "runtime" / "paper")
    assert mcp_runtime({"ANGELBOT_PROFILE": "research"}) == str(ROOT / "runtime" / "research")
    assert mcp_runtime({"ANGELBOT_RUNTIME": "/somewhere/else"}) == "/somewhere/else"              # an explicit folder always wins
    assert json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["angelbot"]["env"]["ANGELBOT_PROFILE"] == "paper"


def test_claude_cannot_start_the_live_services():
    deny = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    for cmd in ("enable angelbot@live", "enable --now angelbot@live", "start angelbot@live", "restart angelbot@live",
                "enable --now angelbot-telegram@live", "start angelbot-telegram@live"):
        assert f"Bash(systemctl --user {cmd}:*)" in deny, cmd
    assert "Bash(./deploy/ab live run:*)" in deny
