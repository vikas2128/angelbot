#!/usr/bin/env python3
"""Build the CSV datasets for the /report dashboard from the journal's closed trades.

Two ways to give it the trades (stdlib only, read-only):
  --db runtime/journal.db        read the SQLite journal directly (run where the file lives)
  --json rows.json               rows copied from the MCP journal_query tool (a list, or {"rows": [...]})

Rows need: exit_ts, net_pnl, gross_pnl, charges, exit_reason, source, symbol, strategy, mode, qty.
`source` is 'session' (a day with a real bot start) or 'replay' (everything else); see SQL below.

Writes trades.csv, by_day.csv, by_reason.csv, totals.csv into --out and prints a summary to
cross-check against the dashboard. vm.csv (setup status) is written by hand by the skill.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

SQL = """
SELECT t.mode, t.symbol, t.strategy, t.qty, t.exit_ts, t.gross_pnl, t.charges, t.net_pnl, t.exit_reason,
       CASE WHEN substr(t.exit_ts,1,10) IN
                 (SELECT substr(ts,1,10) FROM events WHERE kind='start' AND msg LIKE '%replay=False%')
            THEN 'session' ELSE 'replay' END AS source
FROM trades t
WHERE substr(t.exit_ts,1,10) >= :since AND substr(t.exit_ts,1,10) <= :until
ORDER BY t.exit_ts, t.id
"""
FIELDS = ("exit_ts", "net_pnl", "gross_pnl", "charges", "exit_reason", "source", "symbol", "strategy", "mode", "qty")


def query(uri: str, params: dict) -> list[dict]:
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(SQL, params)]
    finally:
        conn.close()


def load_rows(args) -> list[dict]:
    if args.json:
        data = json.loads(Path(args.json).read_text())
        rows = data["rows"] if isinstance(data, dict) else data
        lo, hi = args.since, args.until
        rows = [r for r in rows if lo <= r["exit_ts"][:10] <= hi]
    else:
        if not Path(args.db).exists():
            sys.exit(f"journal not found: {args.db} (run where the file lives, or use --json with MCP rows)")
        params = {"since": args.since, "until": args.until}
        try:
            rows = query(f"file:{args.db}?mode=ro", params)
        except sqlite3.OperationalError:
            # a WAL journal with no -shm next to it can't be opened read-only: query a private copy
            with tempfile.TemporaryDirectory() as tmp:
                for suffix in ("", "-wal", "-shm"):
                    if Path(args.db + suffix).exists():
                        shutil.copy(args.db + suffix, Path(tmp) / ("j.db" + suffix))
                rows = query(f"file:{Path(tmp) / 'j.db'}", params)
    for r in rows:
        missing = [f for f in FIELDS if f not in r]
        if missing:
            sys.exit(f"rows are missing fields: {missing}")
    if args.source != "all":
        rows = [r for r in rows if r["source"] == args.source]
    return rows


def write_csv(path: Path, header: list[str], lines: list[list]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db")
    src.add_argument("--json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--since", default="0000-00-00")
    ap.add_argument("--until", default="9999-99-99")
    ap.add_argument("--source", choices=["all", "session", "replay"], default="all")
    ap.add_argument("--timeframe", default="1-minute", help="candle size label shown on the page")
    args = ap.parse_args()

    rows = load_rows(args)
    if not rows:
        sys.exit("no closed trades match: nothing to report (check --since/--until/--source)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    nets = [round(float(r["net_pnl"]), 2) for r in rows]
    write_csv(out / "trades.csv", ["n", "exit_ts", "net_pnl"],
              [[i, r["exit_ts"][:19], n] for i, (r, n) in enumerate(zip(rows, nets), 1)])

    by_day: dict[str, list] = {}
    for r, n in zip(rows, nets):
        d = by_day.setdefault(r["exit_ts"][:10], [0, 0.0])
        d[0] += 1
        d[1] += n
    write_csv(out / "by_day.csv", ["day", "n", "net"], [[d, v[0], round(v[1], 2)] for d, v in sorted(by_day.items())])

    by_reason: dict[str, list] = {}
    for r, n in zip(rows, nets):
        d = by_reason.setdefault(r["exit_reason"] or "unknown", [0, 0, 0.0, 0.0])
        d[0] += 1
        d[1] += n > 0
        d[2] += float(r["gross_pnl"])
        d[3] += n
    write_csv(out / "by_reason.csv", ["reason", "n", "wins", "gross_pnl", "net_pnl"],
              [[k, v[0], v[1], round(v[2], 2), round(v[3], 2)] for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1][0])])

    gross = round(sum(float(r["gross_pnl"]) for r in rows), 2)
    fees = round(sum(float(r["charges"]) for r in rows), 2)
    net = round(sum(nets), 2)
    wins = sum(n > 0 for n in nets)
    sources = {r["source"] for r in rows}
    basis = sources.pop() if len(sources) == 1 else "mixed"
    uniq = lambda k: ", ".join(sorted({str(r[k]) for r in rows}))
    write_csv(out / "totals.csv",
              ["trades", "wins", "losses", "gross_pnl", "fees", "net_pnl", "days", "first_day", "last_day",
               "symbols", "strategy", "mode", "qty", "timeframe", "basis"],
              [[len(rows), wins, sum(n < 0 for n in nets), gross, fees, net, len(by_day),
                min(by_day), max(by_day), uniq("symbol"), uniq("strategy"), uniq("mode"), uniq("qty"),
                args.timeframe, basis]])

    cum = peak = dd = 0.0
    for n in nets:
        cum += n
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    gw = sum(n for n in nets if n > 0)
    gl = abs(sum(n for n in nets if n < 0))
    print(f"trades {len(rows)} over {len(by_day)} days ({min(by_day)} to {max(by_day)}), basis={basis}")
    print(f"net {net}  gross {gross}  fees {fees}  win rate {wins / len(rows):.1%}  "
          f"profit factor {gw / gl if gl else float('inf'):.2f}  max drawdown {round(dd, 2)}")
    print(f"CSV files written to {out}")


if __name__ == "__main__":
    main()
