"""Screener snapshot -> standalone HTML page and a short Telegram message. No JavaScript, no network.

Same look as the performance report (skills/angelbot-report). Everything is drawn here as plain HTML, so the
file shows its data in a phone's file preview, a mail client or a printout.
"""
from __future__ import annotations

import html
from datetime import datetime

esc = lambda s: html.escape(str(s), quote=True)
DASH = "–"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

CSS = """
:root{--bg:#f7f6f3;--panel:#fff;--fg:#1d1f23;--muted:#666a73;--line:#e3e1db;--accent:#2f5fd0;--bad:#c4382f;--ok:#2a7a4b;--warn:#a86a10}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--panel:#1c1f25;--fg:#e8e9ec;--muted:#9aa0ab;--line:#2c3038;--accent:#7aa2ff;--bad:#ff7d73;--ok:#59c08a;--warn:#e0a24a;color-scheme:dark}}
*{box-sizing:border-box}html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-right,0px)) 40px max(16px,env(safe-area-inset-left,0px));display:flex;flex-direction:column;gap:16px}
h1{font:400 28px/1.15 Georgia,"Iowan Old Style",serif;margin:0 0 4px}h2{font-size:15px;font-weight:600;margin:0}
.sub,.note{color:var(--muted);font-size:13px;margin:0}
.banner{display:flex;gap:10px;padding:10px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--muted);font-size:13px}
.banner i{flex:none;width:8px;height:8px;border-radius:50%;background:var(--warn);margin-top:6px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;min-width:0}
.picks{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,230px),1fr));gap:12px}
.pick .rk{font-size:12px;color:var(--muted)}.pick .sym{font:400 22px/1.2 Georgia,"Iowan Old Style",serif}
.pick .sc{font-size:13px;color:var(--muted);margin:2px 0 8px}.pick .sc b{color:var(--fg);font-size:18px}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;margin-bottom:10px}.bar>div{height:100%;background:var(--accent)}
.pick ul{margin:0 0 10px;padding-left:18px;font-size:13px}
.chips{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px;font-size:12px;color:var(--muted)}.chips b{display:block;color:var(--fg);font-size:14px;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{text-align:left;font-weight:600;color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px;border-bottom:1px solid var(--line)}th.r,td.r{text-align:right;white-space:nowrap}tr:last-child td{border-bottom:0}
.up{color:var(--ok)}.dn{color:var(--bad)}
ul.plain{margin:12px 0 0;padding-left:18px;display:flex;flex-direction:column;gap:6px;font-size:14px}
footer{color:var(--muted);font-size:12px}
@media (max-width:480px){.hide-s{display:none}}
@media print{html,body{background:#fff}.card{break-inside:avoid}}
"""


def _gap(v) -> str:
    return DASH if v is None else f"{v:+.1f}%"


def _pct(v, nd=1) -> str:
    return DASH if v is None else f"{v:.{nd}f}%"


def _num(v, suffix="") -> str:
    return DASH if v is None else f"{v:g}{suffix}"


def date_label(snap: dict) -> str:
    try:
        d = datetime.strptime(snap["market_date"], "%Y-%m-%d")
        return f"{d.strftime('%a')} {d.day} {MONTHS[d.month - 1]}"
    except (KeyError, ValueError):
        return "unknown date"


def how_it_works(p: dict) -> list[str]:
    w = p["weights"]
    tot = sum(float(v) for v in w.values()) or 1.0
    share = lambda k: f"{float(w[k]) / tot * 100:.0f}%"
    return [
        f"A stock is only scored if it passes the gates: at least ₹{float(p['min_traded_value_cr']):.0f} crore traded "
        f"per day on average, a spread of at most {float(p['max_spread_pct']):.2f}% of the price, an average daily range "
        f"at least {float(p['min_range_to_cost']):.0f} times the round-trip fees, and {p['min_history_days']} days of history.",
        f"Fees are worked out for an order of about ₹{float(p['order_value']):,.0f}, since fees as a share of the order "
        "depend on its size.",
        f"The score (0 to 100) ranks each stock against the others on room to move versus fees ({share('range_to_cost')}), "
        f"the size of today's gap ({share('gap')}), how much of a normal day's volume has traded so far ({share('volume')}) "
        f"and liquidity ({share('liquidity')}). A factor with no data is left out for that stock, not guessed.",
        "Volume traded so far is only comparable between stocks at the same moment, and means more later in the morning.",
    ]


def render_html(snap: dict) -> str:
    p = snap["params"]
    ok = [r for r in snap["rows"] if not r["excluded"]]
    bad = [r for r in snap["rows"] if r["excluded"]]
    picks = ok[: int(p["top_n"])]
    cards = "".join(
        f'<div class="card pick"><div class="rk">#{r["rank"]}</div><div class="sym">{esc(r["symbol"])}</div>'
        f'<div class="sc"><b>{r["score"]:.0f}</b> / 100</div><div class="bar"><div style="width:{r["score"]:.0f}%"></div></div>'
        f'<ul>{"".join("<li>" + esc(w) + "</li>" for w in r["why"])}</ul>'
        f'<div class="chips"><span>Price<b>{esc(_num(r["ltp"], ""))}</b></span><span>Gap<b>{esc(_gap(r["gap_pct"]))}</b></span>'
        f'<span>Room vs fees<b>{esc(_num(r["range_to_cost"], "x"))}</b></span><span>Spread<b>{esc(_pct(r["spread_pct"], 2))}</b></span>'
        f'<span>Qty for ₹{float(p["order_value"]):,.0f}<b>{esc(_num(r["suggested_qty"]))}</b></span>'
        f'<span>Traded/day<b>{esc(_num(r["traded_value_cr"], " cr"))}</b></span></div></div>' for r in picks)
    if not picks:
        cards = '<div class="card"><h2>No stock passed the gates today</h2><p class="note">See the excluded list below for why.</p></div>'

    def gap_cell(v):
        cls = "" if v is None else ("up" if v > 0 else "dn" if v < 0 else "")
        return f'<td class="r {cls}">{esc(_gap(v))}</td>'

    rows = "".join(
        f'<tr><td>{r["rank"]}</td><td>{esc(r["symbol"])}</td><td class="r">{r["score"]:.0f}</td>{gap_cell(r["gap_pct"])}'
        f'<td class="r">{esc(_num(r["range_to_cost"], "x"))}</td><td class="r">{esc(_pct(r["vol_pct_of_avg"], 0))}</td>'
        f'<td class="r hide-s">{esc(_pct(r["spread_pct"], 2))}</td><td class="r hide-s">{esc(_num(r["traded_value_cr"]))}</td></tr>'
        for r in ok)
    table = ('<section class="card"><h2>All eligible stocks, ranked</h2><p class="note">Gap is today\'s open against yesterday\'s close. '
             'Volume is the share of a normal day already traded. Traded is ₹ crore a day on average</p><div class="scroll"><table>'
             '<thead><tr><th>#</th><th>Stock</th><th class="r">Score</th><th class="r">Gap</th><th class="r">Room vs fees</th>'
             '<th class="r">Volume</th><th class="r hide-s">Spread</th><th class="r hide-s">Traded cr</th></tr></thead>'
             f"<tbody>{rows}</tbody></table></div></section>") if ok else ""
    excl = ""
    if bad:
        items = "".join(f"<li><b>{esc(r['symbol'])}</b>: {esc('; '.join(r['excluded']))}</li>" for r in bad)
        excl = f'<section class="card"><h2>Left out</h2><p class="note">These failed a gate, so they are not scored</p><ul class="plain">{items}</ul></section>'
    errs = ""
    if snap.get("errors"):
        items = "".join(f"<li>{esc(e)}</li>" for e in snap["errors"])
        errs = f'<section class="card"><h2>Problems collecting data</h2><p class="note">These stocks or fields are missing from the ranking</p><ul class="plain">{items}</ul></section>'
    how = ('<section class="card"><h2>How the score works</h2><ul class="plain">'
           + "".join(f"<li>{esc(t)}</li>" for t in how_it_works(p)) + "</ul></section>")
    gen = str(snap.get("generated_at", ""))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>AngelBot screener {esc(snap.get("market_date", ""))}</title><style>{CSS}</style></head><body><div class="wrap">'
            f'<header><h1>Stock screener</h1><p class="sub">{esc(date_label(snap))} · checked {snap["universe"]} stocks · '
            f'{snap["eligible"]} passed the gates · built {esc(gen[11:16])} IST</p></header>'
            '<div class="banner"><i></i><span>Advisory only. The bot trades just the stocks listed in its config, and this ranking changes '
            'nothing there. The score is a heuristic that has not been backtested: use it to shortlist stocks to test, not as a signal.</span></div>'
            f'<section><div class="picks">{cards}</div></section>{table}{excl}{errs}{how}'
            '<footer>Read-only analytics from market data. No orders are placed by the screener.</footer></div></body></html>')


def summary_html(snap: dict, today: str | None = None) -> str:
    """Short Telegram message (HTML) with the top picks."""
    ok = [r for r in snap["rows"] if not r["excluded"]]
    n = int(snap["params"]["top_n"])
    lines = [f"\U0001F50E <b>Stock screener</b> · {esc(date_label(snap))} {esc(str(snap.get('generated_at', ''))[11:16])} IST"]
    if today and snap.get("market_date") != today:
        lines.append(f"⚠️ This snapshot is from {esc(snap.get('market_date'))}, not today.")
    if not ok:
        lines.append("No stock passed the gates.")
    for r in ok[:n]:
        bits = [f"score {r['score']:.0f}", f"gap {_gap(r['gap_pct'])}", f"room {_num(r['range_to_cost'], 'x')} fees"]
        lines.append(f"{r['rank']}. <b>{esc(r['symbol'])}</b> · " + " · ".join(bits))
    lines.append(f"Checked {snap['universe']} stocks, {snap['eligible']} passed the gates.")
    lines.append("Advisory only: the bot still trades just what is in its config.")
    return "\n".join(lines)
