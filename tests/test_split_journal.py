import importlib.util
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from bot.journal import Journal
from bot.timeutil import IST

SPEC = importlib.util.spec_from_file_location("split_journal", Path(__file__).resolve().parent.parent / "deploy" / "split_journal.py")
sj = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sj)

t = lambda d, h="10:00": datetime.fromisoformat(f"{d} {h}").replace(tzinfo=IST)


class O:                                  # a minimal order record for Journal.order()
    def __init__(self, oid, day):
        self.order_id, self.symbol, self.side, self.qty, self.price, self.purpose = oid, "SBIN-EQ", "BUY", 1, 800.0, "ENTRY"
        self.placed_at, self.status, self.filled_qty, self.avg_fill, self.chases, self.reason = t(day), "complete", 1, 800.0, 0, ""


def mixed_journal(runtime: Path) -> None:
    """Real paper session on 22 Oct; replays of 10 Oct and 21 Oct that were run (and logged) on 22 Oct at 17:49."""
    runtime.mkdir(parents=True, exist_ok=True)
    j = Journal(runtime / "journal.db")
    j.event(t("2026-10-22", "09:05"), "start", "mode=paper replay=False profile=")
    j.event(t("2026-10-22", "10:15"), "feed_restart", "stale feed", "WARNING")
    j.event(t("2026-10-22", "17:49"), "start", "mode=paper replay=True")                       # a replay run today: research
    j.event(t("2026-10-10", "09:20"), "control", "{'paused': False}")                          # replayed day
    for d in ("2026-10-10", "2026-10-21"):
        j.signal(t(d), "SBIN-EQ", 1, 0, 800.0, True, "entry placed")
        j.order(O(f"R-{d}", d), "paper", t(d))
        j.trade("paper", "SBIN-EQ", "LONG", 1, t(d), 800.0, t(d, "10:05"), 797.0, -3.0, 2.2, "stop_loss", "ema_cross")
    j.signal(t("2026-10-22"), "SBIN-EQ", 1, 0, 960.0, True, "entry placed")
    j.order(O("P-1", "2026-10-22"), "paper", t("2026-10-22"))
    j.trade("paper", "SBIN-EQ", "LONG", 1, t("2026-10-22"), 959.5, t("2026-10-22", "10:45"), 964.0, 4.5, 2.6, "target", "ema_cross")
    j.conn.close()


def rows(path: Path, table: str, col: str = "*"):
    con = sqlite3.connect(path)
    try:
        return con.execute(f"SELECT {col} FROM {table}").fetchall()
    finally:
        con.close()


def test_split_puts_each_record_in_its_area_and_loses_nothing(tmp_path):
    rt = tmp_path / "runtime"
    mixed_journal(rt)
    original = [(tb, rows(rt / "journal.db", tb)) for tb in ("events", "signals", "orders", "trades")]
    r = sj.split(rt, today="20261022")
    assert r["areas"]["paper"]["trades"] == 1 and r["areas"]["research"]["trades"] == 2
    paper, research = rt / "paper" / "journal.db", rt / "research" / "replay_journal.db"
    assert [x[0][:10] for x in rows(paper, "trades", "exit_ts")] == ["2026-10-22"]
    assert sorted(x[0][:10] for x in rows(research, "trades", "exit_ts")) == ["2026-10-10", "2026-10-21"]
    assert [m for (m,) in rows(paper, "events", "msg") if m.startswith("mode=")] == ["mode=paper replay=False profile="]
    assert [m for (m,) in rows(research, "events", "msg") if m.startswith("mode=")] == ["mode=paper replay=True"]   # a replay run on a real day
    assert {k for (k,) in rows(paper, "events", "kind")} == {"start", "feed_restart"}
    assert {k for (k,) in rows(research, "events", "kind")} == {"start", "control"}
    for tb, old in original:                                  # every row is somewhere, exactly once
        assert sorted(map(str, rows(paper, tb) + rows(research, tb))) == sorted(map(str, old))
    assert r["areas"]["paper"]["net_pnl"] == pytest.approx(1.9) and r["areas"]["research"]["net_pnl"] == pytest.approx(-10.4)


def test_the_old_files_are_kept_as_a_backup_and_other_files_follow_their_area(tmp_path):
    rt = tmp_path / "runtime"
    mixed_journal(rt)
    old = rows(rt / "journal.db", "trades")
    for name in ("status.json", "control.json", "bot.log", "screen.json", "replay_journal.db"):
        (rt / name).write_text("x")
    (rt / "backtests").mkdir()
    (rt / "status.json").touch()
    import os
    os.utime(rt / "status.json", (time.time() - 3600, time.time() - 3600))                 # an old status file: bot not running
    r = sj.split(rt, today="20261022")
    assert not (rt / "journal.db").exists() and rows(Path(r["legacy"]) / "journal.db", "trades") == old
    assert (rt / "paper" / "status.json").exists() and (rt / "paper" / "control.json").exists() and (rt / "paper" / "bot.log").exists()
    assert (rt / "paper" / "screen.json").exists() and (rt / "research" / "backtests").is_dir()
    assert (rt / "research" / "replay_journal.legacy.db").exists()                          # kept apart from the migrated replay journal
    assert not (rt / "status.json").exists()


def test_a_dry_run_changes_nothing(tmp_path):
    rt = tmp_path / "runtime"
    mixed_journal(rt)
    r = sj.split(rt, dry_run=True)
    assert r["dry_run"] and r["areas"]["paper"]["trades"] == 1
    assert {p.name for p in rt.iterdir()} <= {"journal.db", "journal.db-shm", "journal.db-wal"}
    assert not (rt / "paper").exists() and not (rt / "research").exists()


def test_it_refuses_when_unsafe(tmp_path):
    rt = tmp_path / "runtime"
    with pytest.raises(sj.SplitError, match="nothing to split"):
        sj.split(rt)
    mixed_journal(rt)
    (rt / "status.json").write_text("{}")                                                   # just written: the bot is running
    with pytest.raises(sj.SplitError, match="looks like it is running"):
        sj.split(rt)
    import os
    os.utime(rt / "status.json", (time.time() - 3600, time.time() - 3600))
    (rt / "paper").mkdir()
    (rt / "paper" / "journal.db").write_text("existing")
    with pytest.raises(sj.SplitError, match="refusing to overwrite"):
        sj.split(rt)
    assert (rt / "journal.db").exists()                                                     # untouched


def test_a_failed_check_leaves_everything_as_it_was(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    mixed_journal(rt)
    before = rows(rt / "journal.db", "trades")
    real = sj._copy_rows

    def lossy(src, dest, area):                                                             # drops one trade on the way
        real(src, dest, area)
        if area == "research":
            con = sqlite3.connect(dest); con.execute("DELETE FROM trades WHERE id = (SELECT MIN(id) FROM trades)"); con.commit(); con.close()
    monkeypatch.setattr(sj, "_copy_rows", lossy)
    with pytest.raises(sj.SplitError, match="trades: 3 rows in the old journal but 2 after the split"):
        sj.split(rt, today="20261022")
    assert rows(rt / "journal.db", "trades") == before                                      # the original is still in place
    assert not (rt / "paper" / "journal.db").exists() and not (rt / "research" / "replay_journal.db").exists()
    assert not list(rt.glob("legacy_*"))
