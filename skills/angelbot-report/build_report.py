#!/usr/bin/env python3
"""Build a standalone AngelBot performance report (one HTML file) from the journal's closed trades.

Python standard library only. Read-only. The output opens in any browser: no network, no CDN, no
agent-specific tools. All numbers are worked out here and embedded in the page as JSON.

Give it the trades one of two ways:
  --db runtime/journal.db     read the SQLite journal (run where the file lives, or on a copy)
  --json rows.json            rows from the journal query (a list, or {"rows": [...]}); see --print-sql

Examples:
  python3 build_report.py --db runtime/journal.db --timeframe 1-minute --out report.html
  python3 build_report.py --json rows.json --since 2026-10-01 --source session --out report.html
  python3 build_report.py --print-sql        # the query to run when you only have a query tool
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# `source` marks a trade 'session' (its day has a real bot start) or 'replay' (everything else).
# The journal's `mode` column says 'paper' for both, so it cannot tell them apart.
SQL = """SELECT t.mode, t.symbol, t.strategy, t.qty, t.exit_ts, t.gross_pnl, t.charges, t.net_pnl, t.exit_reason,
       CASE WHEN substr(t.exit_ts,1,10) IN
                 (SELECT substr(ts,1,10) FROM events WHERE kind='start' AND msg LIKE '%replay=False%')
            THEN 'session' ELSE 'replay' END AS source
FROM trades t
WHERE substr(t.exit_ts,1,10) >= :since AND substr(t.exit_ts,1,10) <= :until
ORDER BY t.exit_ts, t.id"""
FIELDS = ("exit_ts", "net_pnl", "gross_pnl", "charges", "exit_reason", "source", "symbol", "strategy", "mode", "qty")
REASONS = {"signal": "Strategy exit signal", "target": "Goal reached", "stop_loss": "Safety exit (stop loss)",
           "squareoff time": "Market-close exit"}


def _query(uri: str, params: dict) -> list[dict]:
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
        rows = [r for r in rows if args.since <= str(r["exit_ts"])[:10] <= args.until]
    else:
        db = Path(args.db)
        if not db.exists():
            sys.exit(f"journal not found: {db} (run where the file lives, or pass rows with --json)")
        params = {"since": args.since, "until": args.until}
        try:
            rows = _query(f"file:{db}?mode=ro", params)
        except sqlite3.OperationalError:
            # a WAL journal with no -shm beside it cannot be opened read-only: query a private copy
            with tempfile.TemporaryDirectory() as tmp:
                for suffix in ("", "-wal", "-shm"):
                    if Path(str(db) + suffix).exists():
                        shutil.copy(str(db) + suffix, Path(tmp) / ("j.db" + suffix))
                rows = _query(f"file:{Path(tmp) / 'j.db'}", params)
    for r in rows:
        missing = [f for f in FIELDS if f not in r]
        if missing:
            sys.exit(f"rows are missing fields: {missing}")
    if args.source != "all":
        rows = [r for r in rows if r["source"] == args.source]
    return rows


def narrate(k: dict, reasons: list[dict], basis: str, mode: str) -> dict:
    """Plain-language reading chosen from the numbers. Words only; figures stay in the cards and charts."""
    if k["net"] > 0:
        take = "The strategy made money after fees."
    elif k["gross"] > 0:
        take = "The strategy made money before fees, but fees turned it into a loss."
    else:
        take = "The strategy lost money even before fees, and fees made it worse."
    if basis == "replay":
        basis_note = "These trades come from replaying saved candles through the bot, not from a live market session."
    elif basis == "session":
        basis_note = ("These trades were placed with real money." if "live" in mode else
                      "These trades come from real bot sessions: live prices with simulated orders, "
                      "so fills are a little optimistic.")
    else:
        basis_note = "This mixes real bot sessions with replayed candles. Read the two separately before drawing conclusions."
    out = ["Fees were larger than the strategy's own result, so costs weighed more than the signals."
           if k["fees"] > abs(k["gross"]) else
           "Fees were smaller than the strategy's own result, so the signals mattered more than costs."]
    by = {r["reason"]: r for r in reasons}
    if "target" in by and by["target"]["wins"] == by["target"]["n"]:
        out.append("Trades that reached the goal always won.")
    if "stop_loss" in by and by["stop_loss"]["wins"] == 0:
        out.append("Trades that hit the safety exit always lost, which is what a stop loss is for.")
    top = max(reasons, key=lambda r: r["n"])
    out.append(f'Most trades ended on "{top["label"].lower()}", and that group '
               f'{"lost money overall." if top["net"] < 0 else "made money overall."}')
    if k["trades"] < 100:
        out.append("Fewer than 100 trades is too few to judge a strategy. Treat this as a baseline.")
    out.append("Next test: change one thing at a time (candle size, exits or filters) and confirm on a later "
               "period the settings never saw.")
    return {"takeaway": take, "basis_note": basis_note, "insights": out}


def build(rows: list[dict], timeframe: str, setup: list[dict] | None) -> dict:
    nets = [round(float(r["net_pnl"]), 2) for r in rows]
    cum = peak = dd = 0.0
    equity = []
    for i, (r, n) in enumerate(zip(rows, nets), 1):
        cum += n
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
        equity.append({"n": i, "ts": str(r["exit_ts"])[:19], "cum": round(cum, 2)})
    days: dict[str, list] = {}
    for r, n in zip(rows, nets):
        d = days.setdefault(str(r["exit_ts"])[:10], [0, 0.0])
        d[0] += 1
        d[1] += n
    reasons: dict[str, list] = {}
    for r, n in zip(rows, nets):
        d = reasons.setdefault(r["exit_reason"] or "unknown", [0, 0, 0.0, 0.0])
        d[0] += 1
        d[1] += n > 0
        d[2] += float(r["gross_pnl"])
        d[3] += n
    by_reason = [{"reason": k, "label": REASONS.get(k, k), "n": v[0], "wins": v[1],
                  "gross": round(v[2], 2), "net": round(v[3], 2)}
                 for k, v in sorted(reasons.items(), key=lambda kv: -kv[1][0])]
    gw = sum(n for n in nets if n > 0)
    gl = abs(sum(n for n in nets if n < 0))
    k = {"net": round(sum(nets), 2), "gross": round(sum(float(r["gross_pnl"]) for r in rows), 2),
         "fees": round(sum(float(r["charges"]) for r in rows), 2), "trades": len(rows),
         "wins": sum(n > 0 for n in nets), "losses": sum(n < 0 for n in nets),
         "win_rate": sum(n > 0 for n in nets) / len(rows),
         "profit_factor": round(gw / gl, 2) if gl else None, "max_dd": round(dd, 2)}
    sources = {r["source"] for r in rows}
    basis = sources.pop() if len(sources) == 1 else "mixed"
    uniq = lambda f: ", ".join(sorted({str(r[f]) for r in rows}))
    meta = {"symbols": uniq("symbol"), "strategy": uniq("strategy"), "mode": uniq("mode"), "qty": uniq("qty"),
            "timeframe": timeframe, "basis": basis, "days": len(days),
            "first_day": min(days), "last_day": max(days),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    return {"meta": meta, "kpis": k, "equity": equity,
            "by_day": [{"day": d, "n": v[0], "net": round(v[1], 2)} for d, v in sorted(days.items())],
            "by_reason": by_reason, "story": narrate(k, by_reason, basis, meta["mode"]), "setup": setup or []}


def render(data: dict, out: Path) -> None:
    template = (HERE / "template.html").read_text()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    if "/*__DATA__*/null" not in template:
        sys.exit("template.html is missing the /*__DATA__*/null placeholder")
    out.write_text(template.replace("/*__DATA__*/null", payload))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--db")
    src.add_argument("--json")
    ap.add_argument("--print-sql", action="store_true", help="print the trades query and exit")
    ap.add_argument("--out", default="angelbot-report.html")
    ap.add_argument("--since", default="0000-00-00")
    ap.add_argument("--until", default="9999-99-99")
    ap.add_argument("--source", choices=["all", "session", "replay"], default="all")
    ap.add_argument("--timeframe", default="1-minute", help="candle size label, e.g. 5-minute")
    ap.add_argument("--setup", help='optional JSON list of {"item","state","detail"} (state: ok|warn|off)')
    args = ap.parse_args()

    if args.print_sql:
        print(SQL)
        return
    if not (args.db or args.json):
        ap.error("give --db or --json (or --print-sql)")
    rows = load_rows(args)
    if not rows:
        sys.exit("no closed trades match: nothing to report (check --since/--until/--source)")
    setup = json.loads(Path(args.setup).read_text()) if args.setup else None
    data = build(rows, args.timeframe, setup)
    render(data, Path(args.out))
    k, m = data["kpis"], data["meta"]
    print(f"trades {k['trades']} over {m['days']} days ({m['first_day']} to {m['last_day']}), basis={m['basis']}")
    print(f"net {k['net']}  gross {k['gross']}  fees {k['fees']}  win rate {k['win_rate']:.1%}  "
          f"profit factor {k['profit_factor']}  max drawdown {k['max_dd']}")
    print(f"report written to {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
