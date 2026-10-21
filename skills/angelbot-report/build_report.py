#!/usr/bin/env python3
"""Build a standalone AngelBot report (one HTML file) from the journal's closed trades.

The report has two tabs: "Today" (the main one: today's trades and what the bot is doing right now) and
"Month" (month to date: results, daily bars, fees, exit reasons). Without JavaScript both are simply
shown one after the other, so the data is visible in any viewer.

Python standard library only. Read-only. The output opens in any browser or file preview: no network,
no CDN, no agent-specific tools. Every figure and chart is drawn here as plain HTML and SVG.

Give it the trades one of two ways:
  --db runtime/journal.db     read the SQLite journal (run where the file lives, or on a copy)
  --json rows.json            rows from the journal query (a list, or {"rows": [...]}); see --print-sql

Examples:
  python3 build_report.py --db runtime/journal.db --status runtime/status.json --out report.html
  python3 build_report.py --json rows.json --since 2026-09-01 --source session --out report.html
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))

# `source` marks a trade 'session' (its day has a real bot start) or 'replay' (everything else).
# The journal's `mode` column says 'paper' for both, so it cannot tell them apart.
SQL = """SELECT t.mode, t.symbol, t.strategy, t.qty, t.side, t.entry_price, t.exit_price, t.exit_ts, t.gross_pnl,
       t.charges, t.net_pnl, t.exit_reason,
       CASE WHEN substr(t.exit_ts,1,10) IN
                 (SELECT substr(ts,1,10) FROM events WHERE kind='start' AND msg LIKE '%replay=False%')
            THEN 'session' ELSE 'replay' END AS source
FROM trades t
WHERE substr(t.exit_ts,1,10) >= :since AND substr(t.exit_ts,1,10) <= :until
ORDER BY t.exit_ts, t.id"""
FIELDS = ("exit_ts", "net_pnl", "gross_pnl", "charges", "exit_reason", "source", "symbol", "strategy", "mode", "qty")
REASONS = {"signal": "Strategy exit signal", "target": "Goal reached", "stop_loss": "Safety exit (stop loss)",
           "squareoff time": "Market-close exit"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]
esc = lambda s: html.escape(str(s), quote=True)


# ---------------------------------------------------------------- reading trades
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


# ---------------------------------------------------------------- numbers and wording
def narrate(k: dict, reasons: list[dict], basis: str, mode: str, today: bool = False) -> dict:
    """Plain-language reading chosen from the numbers. Words only; figures stay in the cards and charts."""
    span = "Today" if today else "The strategy"
    if k["net"] > 0:
        take = f"{span} made money after fees."
    elif k["gross"] > 0:
        take = f"{span} made money before fees, but fees turned it into a loss."
    else:
        take = f"{span} lost money even before fees, and fees made it worse."
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
    if today:
        out.append("One day is a very small sample. Use the Month tab for the bigger picture.")
    else:
        if k["trades"] < 100:
            out.append("Fewer than 100 trades is too few to judge a strategy. Treat this as a baseline.")
        out.append("Next test: change one thing at a time (candle size, exits or filters) and confirm on a later "
                   "period the settings never saw.")
    return {"takeaway": take, "basis_note": basis_note, "insights": out}


def build(rows: list[dict], timeframe: str, setup: list[dict] | None = None, today: bool = False) -> dict:
    """Figures for one set of trades (one tab)."""
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
            "first_day": min(days), "last_day": max(days)}
    trades = [{"time": str(r["exit_ts"])[11:16], "symbol": r["symbol"], "side": r.get("side"),
               "entry": r.get("entry_price"), "exit": r.get("exit_price"), "net": n,
               "reason": REASONS.get(r["exit_reason"], r["exit_reason"] or "unknown")}
              for r, n in zip(rows, nets)]
    return {"meta": meta, "kpis": k, "equity": equity,
            "by_day": [{"day": d, "n": v[0], "net": round(v[1], 2)} for d, v in sorted(days.items())],
            "by_reason": by_reason, "trades": trades, "setup": setup or [],
            "story": narrate(k, by_reason, basis, meta["mode"], today)}


def period(today: str, since: str | None) -> tuple[str, str]:
    """(first day, heading) of the Month tab: the month so far, or from an explicit start date."""
    if since:
        return since, f"Since {MONTHS[int(since[5:7]) - 1]} {int(since[8:10])}"
    return today[:8] + "01", f"{MONTHS_FULL[int(today[5:7]) - 1]} {today[:4]}"


def build_all(rows: list[dict], timeframe: str, today: str, since: str | None = None,
              setup: list[dict] | None = None, status: dict | None = None) -> dict:
    """Both tabs. `rows` are the Month tab's trades (month to date); Today's are the ones that exited today."""
    first, heading = period(today, since)
    month_rows = [r for r in rows if str(r["exit_ts"])[:10] >= first]
    today_rows = [r for r in month_rows if str(r["exit_ts"])[:10] == today]
    d = datetime.strptime(today, "%Y-%m-%d")
    return {"generated": datetime.now(IST).strftime("%Y-%m-%d %H:%M IST"), "today": today,
            "today_label": f"{d.strftime('%a')} {d.day} {MONTHS[d.month - 1]}", "month_label": heading,
            "month_first": first, "timeframe": timeframe,
            "month": build(month_rows, timeframe, setup) if month_rows else None,
            "day": build(today_rows, timeframe, today=True) if today_rows else None,
            "right_now": right_now(status)}


def right_now(status: dict | None) -> list[dict] | None:
    """Plain facts from the bot's status file. Never invents anything: absent file, absent card."""
    if not status or not status.get("written_at"):
        return None
    age = status.get("age_sec")
    if age is None:
        try:
            age = round((datetime.now(IST) - datetime.fromisoformat(status["written_at"])).total_seconds())
        except (ValueError, TypeError):
            age = None
    alive = bool(status["bot_alive"]) if "bot_alive" in status else (
        age is not None and age < 15 and status.get("state") != "stopped")
    out = [{"item": "Bot process", "state": "ok" if alive else "off",
            "detail": "Running" if alive else f"Not running (last update {age if age is not None else '?'}s ago)"}]
    if status.get("replay"):
        out.append({"item": "Last status", "state": "warn", "detail": "From a replay, not a live session"})
    out.append({"item": "Trading mode", "state": "warn" if status.get("mode") == "live" else "ok",
                "detail": str(status.get("mode") or "unknown")})
    for p in status.get("positions") or []:
        out.append({"item": f"Open: {p['symbol']}", "state": "ok",
                    "detail": f"{p['qty']} at {plain(p['avg'])}, now {plain(p['ltp']) if p.get('ltp') else 'n/a'}, "
                              f"so far {money(p.get('unrealized') or 0)}"})
    if not status.get("positions"):
        out.append({"item": "Open positions", "state": "off", "detail": "None"})
    if status.get("halted_reason"):
        out.append({"item": "Stopped for today", "state": "warn", "detail": str(status["halted_reason"])})
    if status.get("cooldown_until"):
        out.append({"item": "Resting after losses", "state": "warn", "detail": f"Until {str(status['cooldown_until'])[11:16]}"})
    if (status.get("control") or {}).get("paused"):
        out.append({"item": "New trades", "state": "warn", "detail": "Paused"})
    return out


# ---------------------------------------------------------------- rendering (no JavaScript needed)
# Everything is drawn here, as plain HTML and inline SVG, so the file shows its data in any viewer:
# a phone's file preview, a mail client, a browser with scripts off, or a printout.
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
    return ("−" if v < 0 else "+" if v > 0 else "") + "₹" + inr(v)


def plain(v: float) -> str:
    return "₹" + inr(float(v))


def day_label(s: str) -> str:
    return f"{MONTHS[int(s[5:7]) - 1]} {int(s[8:10])}"


def axis(v: float) -> str:
    return ("−" if v < 0 else "") + "₹" + f"{abs(v):g}"


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
    for n in sorted({round(maxn * i / 3) for i in range(1, 4)}):
        o.append(f'<text class="t" x="{X(n):.1f}" y="{H - 8}" text-anchor="middle">Trade {n}</text>')
    o.append('<path class="ln" d="' + " ".join(f'{"L" if i else "M"}{X(n):.1f} {Y(c):.1f}' for i, (n, c, _) in enumerate(pts)) + '"/>')
    n, c, _ = pts[-1]
    o.append(f'<circle class="dt" cx="{X(n):.1f}" cy="{Y(c):.1f}" r="4"/>'
             f'<text class="tb" x="{X(n) + 8:.1f}" y="{Y(c) + 4:.1f}">{esc(money(c))}</text>')
    for n, c, ts in pts[1:]:                                  # native hover text where the viewer has a pointer
        when = f"{day_label(ts[:10])}, {ts[11:16]} · " if ts else ""
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
                 f'<title>{esc(day_label(r["day"]))} · {r["n"]} trades: {esc(money(r["net"]))}</title></rect>')
        if i % every == 0:
            o.append(f'<text class="t" x="{x0 + w / 2:.1f}" y="{H - 12}" text-anchor="middle">{esc(day_label(r["day"]))}</text>')
    o.append(f'<line class="ax" x1="{ml}" x2="{W - mr}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/></svg>')
    return "".join(o)


def kpis_html(k: dict) -> str:
    cards = [("Net result", money(k["net"]), "After all fees", k["net"] < 0),
             ("Trades", str(k["trades"]), "Closed round trips", False),
             ("Win rate", f"{k['win_rate'] * 100:.1f}%", "Trades with a net profit", False),
             ("Profit factor", "n/a" if k["profit_factor"] is None else f"{k['profit_factor']:.2f}",
              "Net wins ÷ net losses. Above 1 is profitable", False),
             ("Max drawdown", money(k["max_dd"]), "Biggest fall from a peak", k["max_dd"] < 0),
             ("Fees paid", "−" + plain(k["fees"]), "Brokerage, taxes and charges", False)]
    return '<section class="kpis">' + "".join(
        f'<div class="card kpi"><div class="l">{esc(l)}</div><div class="v{" bad" if bad else ""}">{esc(v)}</div>'
        f'<div class="n">{esc(n)}</div></div>' for l, v, n, bad in cards) + "</section>"


def fees_html(k: dict) -> str:
    mx = max(abs(k["gross"]), abs(k["fees"]), abs(k["net"])) or 1
    return "".join(
        f'<div class="bar"><span class="lab">{l}</span><div class="track"><div class="fill {cls}" style="width:{abs(v) / mx * 100:.1f}%"></div></div>'
        f'<span class="num">{esc(t)}</span></div>'
        for l, v, t, cls in [("Gross result", k["gross"], money(k["gross"]), "f-muted"),
                             ("Fees paid", k["fees"], "−" + plain(k["fees"]), "f-bad"),
                             ("Net result", k["net"], money(k["net"]), "f-bad" if k["net"] < 0 else "f-acc")])


def reasons_html(by_reason: list[dict]) -> str:
    return "".join("<tr>" + f'<td>{esc(r["label"])}</td><td class="r">{r["n"]}</td><td class="r">{r["wins"]}</td>'
                   f'<td class="r hide-s">{esc(money(r["gross"]))}</td><td class="r">{esc(money(r["net"]))}</td></tr>'
                   for r in by_reason)


def status_card(title: str, note: str, rows: list[dict]) -> str:
    body = "".join(f'<tr><td>{esc(r["item"])}</td><td><span class="pill {esc(r["state"])}">'
                   f'{esc({"ok": "OK", "warn": "Attention", "off": "Off"}.get(r["state"], r["state"]))}</span></td>'
                   f'<td>{esc(r["detail"])}</td></tr>' for r in rows)
    return (f'<div class="card"><h2>{esc(title)}</h2><p class="note">{esc(note)}</p><div class="scroll"><table>'
            f'<thead><tr><th>Item</th><th>State</th><th>Detail</th></tr></thead><tbody>{body}</tbody></table></div></div>')


def chart_card(title: str, note: str, inner: str) -> str:
    return f'<section class="card"><h2>{esc(title)}</h2><p class="note">{esc(note)}</p><div class="chart">{inner}</div></section>'


def insights_card(story: dict) -> str:
    return ('<div class="card"><h2>What this tells us</h2><p class="note">Plain reading of the numbers above</p><ul>'
            + "".join(f"<li>{esc(t)}</li>" for t in story["insights"]) + "</ul></div>")


def sub_line(m: dict) -> str:
    return " · ".join([m["symbols"], f'{m["timeframe"]} candles', m["strategy"], f'{m["qty"]} per order',
                            f'{m["mode"]} mode'])


def today_panel(d: dict) -> str:
    out = [f'<h2 class="ph">Today · {esc(d["today_label"])}</h2>']
    day = d["day"]
    if day is None:
        out.append('<div class="card"><h2>No closed trades today yet</h2><p class="note">Trades appear here once '
                   'they close. The Month tab has the bigger picture.</p></div>')
    else:
        out.append(f'<p class="take">{esc(day["story"]["takeaway"])}</p>'
                   f'<div class="banner"><i></i><span>{esc(day["story"]["basis_note"])}</span></div>')
        out.append(kpis_html(day["kpis"]))
        out.append(chart_card("Running net result today", "Cumulative result after each closed trade, after fees",
                              svg_equity(day["equity"])))
        dash = "\u2013"

        def trade_row(t: dict) -> str:
            side = str(t["side"] or "").title() or dash
            legs = plain(t["entry"]) + " \u2192 " + plain(t["exit"]) if t["entry"] and t["exit"] else dash
            return (f'<tr><td>{esc(t["time"])}</td><td>{esc(side)}</td><td class="hide-s">{esc(legs)}</td>'
                    f'<td class="r">{esc(money(t["net"]))}</td><td>{esc(t["reason"])}</td></tr>')

        rows = "".join(trade_row(t) for t in day["trades"])
        out.append('<section class="card"><h2>Trades today</h2><p class="note">Time is when the trade closed (IST). '
                   'Net is after fees</p><div class="scroll"><table><thead><tr><th>Closed</th><th>Side</th>'
                   '<th class="hide-s">In → out</th><th class="r">Net</th><th>How it ended</th></tr></thead>'
                   f"<tbody>{rows}</tbody></table></div></section>")
        out.append('<section class="grid">' + insights_card(day["story"]) + "</section>")
    if d["right_now"]:
        out.append('<section class="grid">' + status_card("Right now", "From the bot's status file", d["right_now"]) + "</section>")
    return "".join(out)


def month_panel(d: dict) -> str:
    out = [f'<h2 class="ph">Month · {esc(d["month_label"])}</h2>']
    mo = d["month"]
    if mo is None:
        out.append('<div class="card"><h2>No closed trades in this period yet</h2></div>')
        return "".join(out)
    m, k = mo["meta"], mo["kpis"]
    out.append(f'<p class="take">{esc(mo["story"]["takeaway"])}</p>'
               f'<p class="sub">{esc(sub_line(m))} · {esc(day_label(m["first_day"]))} to '
               f'{esc(day_label(m["last_day"]))} · {m["days"]} trading days</p>'
               f'<div class="banner"><i></i><span>{esc(mo["story"]["basis_note"])}</span></div>')
    out.append(kpis_html(k))
    out.append(chart_card("Running net result", "Cumulative result after each closed trade, after fees", svg_equity(mo["equity"])))
    out.append('<section class="grid">'
               + chart_card("Net result by day", "Each bar is one trading day", svg_days(mo["by_day"])).replace("<section", "<div").replace("</section>", "</div>")
               + f'<div class="card"><h2>Where the money went</h2><p class="note">Gross result, fees and net result for all trades</p><div class="bars">{fees_html(k)}</div></div></section>')
    out.append('<section class="card"><h2>How trades ended</h2><p class="note">Goal means the profit target was reached. '
               'Safety exit means the stop loss was hit</p><div class="scroll"><table><thead><tr><th>Exit reason</th>'
               '<th class="r">Trades</th><th class="r">Won</th><th class="r hide-s">Gross</th><th class="r">Net after fees</th></tr></thead>'
               f"<tbody>{reasons_html(mo['by_reason'])}</tbody></table></div></section>")
    out.append('<section class="grid">' + insights_card(mo["story"])
               + (status_card("Setup status", "Snapshot taken when this report was made", mo["setup"]) if mo["setup"] else "")
               + "</section>")
    return "".join(out)


def render_parts(d: dict) -> dict[str, str]:
    return {"TITLE": esc(f'AngelBot report · {d["today"]}'),
            "TODAY": today_panel(d), "MONTH": month_panel(d),
            "FOOT": esc(f'Generated {d["generated"]}. Read-only analytics from the bot journal. Net figures are after charges.')}


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
    ap.add_argument("--since", help="start of the Month tab, YYYY-MM-DD (default: the 1st of this month)")
    ap.add_argument("--today", help="treat this date as today, YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--source", choices=["all", "session", "replay"], default="all")
    ap.add_argument("--timeframe", default="1-minute", help="candle size label, e.g. 5-minute")
    ap.add_argument("--status", help="optional runtime/status.json: adds the 'Right now' card to the Today tab")
    ap.add_argument("--setup", help='optional JSON list of {"item","state","detail"} (state: ok|warn|off) for the Month tab')
    args = ap.parse_args()

    if args.print_sql:
        print(SQL)
        return
    if not (args.db or args.json):
        ap.error("give --db or --json (or --print-sql)")
    today = args.today or datetime.now(IST).strftime("%Y-%m-%d")
    user_since = args.since
    first, _ = period(today, user_since)
    args.since, args.until = first, "9999-99-99"
    rows = load_rows(args)
    if not rows:
        sys.exit(f"no closed trades from {first} on: nothing to report (check --since/--source)")
    setup = json.loads(Path(args.setup).read_text()) if args.setup else None
    status = json.loads(Path(args.status).read_text()) if args.status and Path(args.status).exists() else None
    data = build_all(rows, args.timeframe, today, user_since, setup, status)
    render(data, Path(args.out))
    for name, sec in (("today", data["day"]), ("month", data["month"])):
        if sec is None:
            print(f"{name}: no closed trades")
            continue
        k, m = sec["kpis"], sec["meta"]
        print(f"{name}: trades {k['trades']} ({m['first_day']} to {m['last_day']}, basis={m['basis']})  net {k['net']}  "
              f"gross {k['gross']}  fees {k['fees']}  win rate {k['win_rate']:.1%}  "
              f"profit factor {k['profit_factor']}  max drawdown {k['max_dd']}")
    print(f"report written to {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
