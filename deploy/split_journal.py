#!/usr/bin/env python3
"""One-off migration: split the old shared journal into the paper and research areas.

Before the areas existed, replays and real paper sessions were written to the same runtime/journal.db. This copies
each record to the area it belongs to, checks that nothing was lost, and only then moves the old files into a
backup folder. It never deletes anything.

  python3 deploy/split_journal.py --dry-run          # show what would happen, write nothing
  python3 deploy/split_journal.py                    # do it (stop the bot and the Telegram service first)

How a record is classified (the same rule the report uses): a day is a REAL SESSION day if the journal has a `start`
event for it with `replay=False`. Trades, orders, signals and events dated on such a day are PAPER; everything else is
RESEARCH (replay). `start` events say it themselves (`replay=True` / `replay=False`).
Real paper records go to runtime/paper/journal.db; replay records to runtime/research/replay_journal.db.
Edge case: a replay of a date that also had a real session cannot be told apart by date and is counted as paper.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.journal import Journal  # noqa: E402  (creates the schema)

TABLES = {"events": "ts", "signals": "ts", "orders": "ts", "trades": "exit_ts"}
AREA_DB = {"paper": "paper/journal.db", "research": "research/replay_journal.db"}
# runtime files that belong to the running paper bot / to research, moved into their area
PAPER_FILES = ["status.json", "control.json", "bot.log", "screen.json", "selection_request.json",
               "selection_choice.json", "selection_state.json"]
RESEARCH_FILES = ["replay_journal.db", "backtests"]


class SplitError(Exception):
    pass


def _area_expr(table: str) -> str:
    """SQL that yields 'paper' or 'research' for a row of `table` (needs the temp table session_days)."""
    by_date = f"substr({TABLES[table]},1,10) IN (SELECT d FROM session_days)"
    if table == "events":                       # `start` events say it themselves; other events go by their date
        return ("CASE WHEN kind='start' AND msg LIKE '%replay=True%' THEN 'research' "
                "WHEN kind='start' AND msg LIKE '%replay=False%' THEN 'paper' "
                f"WHEN {by_date} THEN 'paper' ELSE 'research' END")
    return f"CASE WHEN {by_date} THEN 'paper' ELSE 'research' END"


def _copy_rows(src: Path, dest: Path, area: str) -> None:
    con = sqlite3.connect(str(dest), isolation_level=None)
    try:
        con.execute("ATTACH ? AS src", (str(src),))
        con.execute("CREATE TEMP TABLE session_days(d TEXT)")
        con.execute("INSERT INTO session_days SELECT DISTINCT substr(ts,1,10) FROM src.events "
                    "WHERE kind='start' AND msg LIKE '%replay=False%'")
        for table in TABLES:
            con.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table} WHERE {_area_expr(table)} = ?", (area,))
    finally:
        con.close()


def _counts(path: Path) -> dict:
    con = sqlite3.connect(str(path))
    try:
        out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        out["net_pnl"] = round(con.execute("SELECT COALESCE(SUM(net_pnl),0) FROM trades").fetchone()[0], 2)
        return out
    finally:
        con.close()


def _copy_journal(db: Path, work: Path) -> Path:
    """Private working copy of the journal (and its WAL files) so the original is never opened for writing."""
    for suffix in ("", "-wal", "-shm"):
        if Path(str(db) + suffix).exists():
            shutil.copy2(str(db) + suffix, work / ("journal.db" + suffix))
    return work / "journal.db"


def split(runtime: Path, today: str | None = None, dry_run: bool = False, now_fn=time.time) -> dict:
    """Split runtime/journal.db into the areas. Returns a report. Raises SplitError (nothing is changed on failure)."""
    src_db = runtime / "journal.db"
    if not src_db.exists():
        raise SplitError(f"no journal at {src_db}: nothing to split")
    status = runtime / "status.json"
    if status.exists() and now_fn() - status.stat().st_mtime < 120:
        raise SplitError("status.json was updated in the last two minutes: the bot looks like it is running. "
                         "Stop angelbot and angelbot-telegram first.")
    dests = {a: runtime / rel for a, rel in AREA_DB.items()}
    clash = [str(p) for p in dests.values() if p.exists()]
    if clash:
        raise SplitError("destination already exists, refusing to overwrite: " + ", ".join(clash))
    legacy = runtime / f"legacy_{today or datetime.now().strftime('%Y%m%d')}"
    if legacy.exists():
        raise SplitError(f"{legacy} already exists, refusing to overwrite")

    with tempfile.TemporaryDirectory() as tmp:
        work = _copy_journal(src_db, Path(tmp))
        original = _counts(work)
        created: list[Path] = []
        try:
            report = {"original": original, "areas": {}}
            for area, dest in dests.items():
                if dry_run:
                    probe = Path(tmp) / f"{area}.db"
                    Journal(probe).conn.close()
                    _copy_rows(work, probe, area)
                    report["areas"][area] = _counts(probe)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                Journal(dest).conn.close()
                created.append(dest)
                _copy_rows(work, dest, area)
                report["areas"][area] = _counts(dest)
            for table in TABLES:                                       # nothing lost, nothing duplicated
                got = sum(r[table] for r in report["areas"].values())
                if got != original[table]:
                    raise SplitError(f"{table}: {original[table]} rows in the old journal but {got} after the split")
            net = round(sum(r["net_pnl"] for r in report["areas"].values()), 2)
            if abs(net - original["net_pnl"]) > 0.005:
                raise SplitError(f"net P&L changed: {original['net_pnl']} before, {net} after")
        except Exception:
            for p in created:                                          # leave no half-made areas behind
                for suffix in ("", "-wal", "-shm"):
                    Path(str(p) + suffix).unlink(missing_ok=True)
            raise
    report["dry_run"] = dry_run
    if dry_run:
        return report
    legacy.mkdir()
    for suffix in ("", "-wal", "-shm"):                                # the old files move, as a backup, only after the checks
        f = Path(str(src_db) + suffix)
        if f.exists():
            shutil.move(str(f), str(legacy / f.name))
    report["legacy"] = str(legacy)
    report["moved"] = move_files(runtime)
    return report


def move_files(runtime: Path) -> list[str]:
    """Move the other runtime files into their area. Never overwrites."""
    moved = []
    for names, area in ((PAPER_FILES, "paper"), (RESEARCH_FILES, "research")):
        for name in names:
            src = runtime / name
            if not src.exists():
                continue
            dest_name = "replay_journal.legacy.db" if name == "replay_journal.db" else name
            dest = runtime / area / dest_name
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved.append(f"{name} -> {area}/{dest_name}")
    return moved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runtime", default=str(ROOT / "runtime"), help="the old runtime folder (default: ./runtime)")
    ap.add_argument("--dry-run", action="store_true", help="work out the split and print it; write nothing")
    args = ap.parse_args()
    try:
        r = split(Path(args.runtime), dry_run=args.dry_run)
    except SplitError as e:
        sys.exit(f"stopped, nothing was changed: {e}")
    print("old journal:", r["original"])
    for area, c in r["areas"].items():
        print(f"  {area:<9} {c}")
    if r["dry_run"]:
        print("dry run only: nothing was written")
        return
    print(f"old files moved to {r['legacy']} (kept as the backup)")
    for m in r["moved"]:
        print("  moved", m)
    print("counts and net P&L match. Next: start the paper services (see docs/ENVIRONMENTS.md)")


if __name__ == "__main__":
    main()
