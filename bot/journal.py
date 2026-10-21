"""SQLite journal of signals, orders, round-trip trades and events."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events  (ts TEXT, level TEXT, kind TEXT, msg TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS signals (ts TEXT, symbol TEXT, desired INTEGER, side_before INTEGER,
                                    price REAL, acted INTEGER, reason TEXT);
CREATE TABLE IF NOT EXISTS orders  (order_id TEXT PRIMARY KEY, ts TEXT, mode TEXT, symbol TEXT, side TEXT,
                                    qty INTEGER, price REAL, purpose TEXT, status TEXT, filled_qty INTEGER,
                                    avg_fill REAL, chases INTEGER, reason TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS trades  (id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, symbol TEXT, side TEXT,
                                    qty INTEGER, entry_ts TEXT, entry_price REAL, exit_ts TEXT,
                                    exit_price REAL, gross_pnl REAL, charges REAL, net_pnl REAL,
                                    exit_reason TEXT, strategy TEXT);
CREATE INDEX IF NOT EXISTS idx_trades_exit ON trades(exit_ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _x(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.conn.execute(sql, args)

    def event(self, ts: datetime, kind: str, msg: str, level: str = "INFO", data: dict | None = None):
        self._x("INSERT INTO events VALUES (?,?,?,?,?)",
                (ts.isoformat(), level, kind, msg, json.dumps(data or {}, default=str)))

    def signal(self, ts, symbol, desired, side_before, price, acted, reason):
        self._x("INSERT INTO signals VALUES (?,?,?,?,?,?,?)",
                (ts.isoformat(), symbol, desired, side_before, price, int(acted), reason))

    def order(self, o, mode: str, now: datetime):
        self._x(
            """INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, filled_qty=excluded.filled_qty,
               avg_fill=excluded.avg_fill, chases=excluded.chases, price=excluded.price,
               reason=excluded.reason, updated=excluded.updated""",
            (o.order_id, o.placed_at.isoformat(), mode, o.symbol, o.side, o.qty, o.price, o.purpose,
             o.status, o.filled_qty, o.avg_fill, o.chases, o.reason, now.isoformat()),
        )

    def trade(self, mode, symbol, side, qty, entry_ts, entry_price, exit_ts, exit_price,
              gross, charges, exit_reason, strategy):
        self._x(
            """INSERT INTO trades (mode,symbol,side,qty,entry_ts,entry_price,exit_ts,exit_price,
               gross_pnl,charges,net_pnl,exit_reason,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mode, symbol, side, qty, entry_ts.isoformat() if entry_ts else None, entry_price,
             exit_ts.isoformat(), exit_price, round(gross, 2), round(charges, 2),
             round(gross - charges, 2), exit_reason, strategy),
        )


def query_readonly(path: Path, sql: str, limit: int = 200) -> list[dict]:
    """Run a read-only SELECT (used by the MCP server). Writes are impossible: DB opened mode=ro."""
    s = sql.strip().rstrip(";")
    if not s.lower().startswith(("select", "with")) or ";" in s:
        raise ValueError("Only a single SELECT/WITH statement is allowed")
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(s).fetchmany(limit)
        return [dict(r) for r in rows]
    finally:
        conn.close()
