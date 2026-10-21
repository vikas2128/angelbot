#!/usr/bin/env python3
"""Build a standalone AngelBot performance report (one HTML file) from the journal's closed trades.

Python standard library only. Read-only. The output opens in any browser or file preview: no network,
no CDN, no JavaScript, no agent-specific tools. Every figure and chart is drawn here as plain HTML and SVG.

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
import html
import json
import math
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


# ---------------------------------------------------------------- rendering (no JavaScript needed)
# Everything is drawn here, as plain HTML and inline SVG, so the file shows its data in any viewer:
# a phone's file preview, a mail client, a browser with scripts off, or a printout.
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
esc = lambda s: html.escape(str(s), quote=True)


def inr(v: float) -> str:
    """12,34,567.89 (Indian digit grouping)."""
    whole, frac = f"{abs(v):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + parts + [tail])
    return f"{whole}.{frac}"


def money(v: float) -> str:
    return ("\u2212" if v < 0 else "+" if v > 0 else "") + "\u20b9" + inr(v)


def plain(v: float) -> str:
    return "\u20b9" + inr(v)


def day_label(s: str) -> str:
    return f"{MONTHS[int(s[5:7]) - 1]} {int(s[8:10])}"


def axis(v: float) -> str:
    return ("\u2212" if v < 0 else "") + "\u20b9" + f"{abs(v):g}"


def nice_ticks(lo: float, hi: float, count: int) -> list[float]:
    span = (hi - lo) or 1.0
    raw = span / count
    mag = 10 ** math.floor(math.log10(raw))
    err = raw / mag
    step = mag * (10 if err >= 7.5 else 5 if err >= 3.5 else 2 if err >= 1.5 else 1)
    v = math.floor(lo / step) * step
    out = []
    while v <= math.ceil(hi / step) * step + step / 2:
        out.append(round(v, 6))
        v += step
    return out


def svg_equity(equity: list[dict]) -> str:
    W, H, ml, mr, mt, mb = 440, 250, 58, 68, 12, 30
    pts = [(0, 0.0, "")] + [(p["n"], p["cum"], p["ts"]) for p in equity]
    ticks = nice_ticks(min(0, min(c for _, c, _ in pts)), max(0, max(c for _, c, _ in pts)), 5)
    lo, hi = ticks[0], ticks[-1]
    maxn = pts[-1][0] or 1
    X = lambda n: ml + n / maxn * (W - ml - mr)
    Y = lambda v: mt + (hi - v) / ((hi - lo) or 1) * (H - mt - mb)
    o = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Running net result after each trade">']
    for t in ticks:
        o.append(f'<line class="gl" x1="{ml}" x2="{W - mr}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                 f'<text class="t" x="{ml - 8}" y="{Y(t) + 4:.1f}" text-anchor="end">{esc(axis(t))}</text>')
    o.append(f'<line class="zl" x1="{ml}" x2="{W - mr}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    for i in range(1, 4):
        n = round(maxn * i / 3)
        o.append(f'<text class="t" x="{X(n):.1f}" y="{H - 8}" text-anchor="middle">Trade {n}</text>')
    o.append('<path class="ln" d="' + " ".join(f'{"L" if i else "M"}{X(n):.1f} {Y(c):.1f}' for i, (n, c, _) in enumerate(pts)) + '"/>')
    n, c, _ = pts[-1]
    o.append(f'<circle class="dt" cx="{X(n):.1f}" cy="{Y(c):.1f}" r="4"/>'
             f'<text class="tb" x="{X(n) + 8:.1f}" y="{Y(c) + 4:.1f}">{esc(money(c))}</text>')
    for n, c, ts in pts[1:]:                                  # native hover text where the viewer has a pointer
        when = f"{day_label(ts[:10])}, {ts[11:16]} \u00b7 " if ts else ""
        o.append(f'<circle class="hit" cx="{X(n):.1f}" cy="{Y(c):.1f}" r="6"><title>{esc(when)}trade {n}: {esc(money(c))} running</title></circle>')
    o.append("</svg>")
    return "".join(o)


def svg_days(rows: list[dict]) -> str:
    W, H, ml, mr, mt, mb = 440, 240, 56, 6, 10, 34
    ticks = nice_ticks(min(0, min(r["net"] for r in rows)), max(0, max(r["net"] for r in rows)), 4)
    lo, hi = ticks[0], ticks[-1]
    Y = lambda v: mt + (hi - v) / ((hi - lo) or 1) * (H - mt - mb)
    bw = (W - ml - mr) / len(rows)
    pad = bw * 0.22
    every = max(1, math.ceil(len(rows) / 5))
    o = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Net result per trading day">']
    for t in ticks:
        o.append(f'<line class="gl" x1="{ml}" x2="{W - mr}" y1="{Y(t):.1f}" y2="{Y(t):.1f}"/>'
                 f'<text class="t" x="{ml - 8}" y="{Y(t) + 4:.1f}" text-anchor="end">{esc(axis(t))}</text>')
    for i, r in enumerate(rows):
        x0, w = ml + i * bw + pad, max(2.0, bw - 2 * pad)
        top, h = min(Y(r["net"]), Y(0)), max(1.0, abs(Y(r["net"]) - Y(0)))
        o.append(f'<rect class="{"bn" if r["net"] < 0 else "bp"}" x="{x0:.1f}" y="{top:.1f}" width="{w:.1f}" height="{h:.1f}" rx="3">'
                 f'<title>{esc(day_label(r["day"]))} \u00b7 {r["n"]} trades: {esc(money(r["net"]))}</title></rect>')
        if i % every == 0:
            o.append(f'<text class="t" x="{x0 + w / 2:.1f}" y="{H - 12}" text-anchor="middle">{esc(day_label(r["day"]))}</text>')
    o.append(f'<line class="ax" x1="{ml}" x2="{W - mr}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/></svg>')
    return "".join(o)


def render_parts(d: dict) -> dict[str, str]:
    m, k, story = d["meta"], d["kpis"], d["story"]
    cards = [("Net result", money(k["net"]), "After all fees", k["net"] < 0),
             ("Trades", str(k["trades"]), "Closed round trips", False),
             ("Win rate", f"{k['win_rate'] * 100:.1f}%", "Trades with a net profit", False),
             ("Profit factor", "n/a" if k["profit_factor"] is None else f"{k['profit_factor']:.2f}",
              "Net wins \u00f7 net losses. Above 1 is profitable", False),
             ("Max drawdown", money(k["max_dd"]), "Biggest fall from a peak", k["max_dd"] < 0),
             ("Fees paid", "\u2212" + plain(k["fees"]), "Brokerage, taxes and charges", False)]
    kpis = "".join(f'<div class="card kpi"><div class="l">{esc(l)}</div><div class="v{" bad" if bad else ""}">{esc(v)}</div>'
                   f'<div class="n">{esc(n)}</div></div>' for l, v, n, bad in cards)
    mx = max(abs(k["gross"]), abs(k["fees"]), abs(k["net"])) or 1
    fees = "".join(
        f'<div class="bar"><span class="lab">{l}</span><div class="track"><div class="fill {cls}" style="width:{abs(v) / mx * 100:.1f}%"></div></div>'
        f'<span class="num">{esc(t)}</span></div>'
        for l, v, t, cls in [("Gross result", k["gross"], money(k["gross"]), "f-muted"),
                             ("Fees paid", k["fees"], "\u2212" + plain(k["fees"]), "f-bad"),
                             ("Net result", k["net"], money(k["net"]), "f-bad" if k["net"] < 0 else "f-acc")])
    reasons = "".join("<tr>" + f'<td>{esc(r["label"])}</td><td class="r">{r["n"]}</td><td class="r">{r["wins"]}</td>'
                      f'<td class="r hide-s">{esc(money(r["gross"]))}</td><td class="r">{esc(money(r["net"]))}</td>' + "</tr>"
                      for r in d["by_reason"])
    setup = ""
    if d["setup"]:
        rows = "".join(f'<tr><td>{esc(r["item"])}</td><td><span class="pill {esc(r["state"])}">'
                       f'{esc({"ok": "OK", "warn": "Attention", "off": "Off"}.get(r["state"], r["state"]))}</span></td>'
                       f'<td>{esc(r["detail"])}</td></tr>' for r in d["setup"])
        setup = ('<div class="card"><h2>Setup status</h2><p class="note">Snapshot taken when this report was made</p>'
                 '<div class="scroll"><table><thead><tr><th>Item</th><th>State</th><th>Detail</th></tr></thead>'
                 f"<tbody>{rows}</tbody></table></div></div>")
    sub = " \u00b7 ".join([m["symbols"], f'{m["timeframe"]} candles', m["strategy"], f'{m["qty"]} per order',
                          f'{m["mode"]} mode', f'{day_label(m["first_day"])} to {day_label(m["last_day"])}',
                          f'{m["days"]} trading days'])
    return {"TITLE": esc(f'AngelBot report {m["first_day"]} to {m["last_day"]}'), "TAKE": esc(story["takeaway"]),
            "SUB": esc(sub), "BASIS": esc(story["basis_note"]), "KPIS": kpis, "EQUITY": svg_equity(d["equity"]),
            "DAYS": svg_days(d["by_day"]), "FEES": fees, "REASONS": reasons,
            "INSIGHTS": "".join(f"<li>{esc(t)}</li>" for t in story["insights"]), "SETUP": setup,
            "FOOT": esc(f'Generated {m["generated"]}. Read-only analytics from the bot journal. Net figures are after charges.')}


def render(data: dict, out: Path) -> None:
    page = (HERE / "template.html").read_text()
    parts = render_parts(data)
    # data for tools and agents; an inert JSON block, never executed. "</" is escaped so it cannot end the tag.
    parts["JSON"] = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    for key, value in parts.items():
        marker = f"<!--@{key}@-->"
        if marker not in page:
            sys.exit(f"template.html is missing the {marker} placeholder")
        page = page.replace(marker, value)
    out.write_text(page)


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
