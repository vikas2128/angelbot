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
.banner.decided{color:var(--fg);border-left:3px solid var(--accent)}
.banner.decided i{background:var(--accent)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin:10px 0 8px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 12px}.kpi .l{font-size:12px;color:var(--muted)}
.kpi .v{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.whys{display:flex;flex-direction:column;gap:8px;margin-top:12px}
.why{display:grid;grid-template-columns:1fr auto;gap:2px 10px;font-size:13px}.why b{font-variant-numeric:tabular-nums}
.why .bar{grid-column:1/-1;margin:0}
h3.grp{font-size:13px;font-weight:600;margin:14px 0 0;display:flex;gap:6px}h3.grp span{color:var(--muted);font-weight:400}
ul.plain.tight{margin-top:6px;gap:3px}.why-d{color:var(--muted)}
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
.tag{display:inline-block;font-size:10px;font-weight:600;letter-spacing:.03em;padding:0 5px;margin-left:4px;border:1px solid var(--line);border-radius:4px;color:var(--muted);vertical-align:middle;line-height:16px}
.tab-in{position:absolute;opacity:0;width:1px;height:1px;pointer-events:none}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tabs label{color:var(--muted);font:600 13px/1 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:8px 12px;border:1px solid var(--line);border-radius:999px;cursor:pointer;-webkit-tap-highlight-color:transparent}
.panel{display:none}
.tabhint{color:var(--muted);font-size:12px;margin:8px 0 0}
@media (max-width:480px){.hide-s{display:none}}
@media print{html,body{background:#fff}.card{break-inside:avoid}.tabs,.tabhint{display:none}.panel{display:block!important;margin-bottom:12px}}
"""


def _gap(v) -> str:
    return DASH if v is None else f"{v:+.1f}%"


def _pct(v, nd=1) -> str:
    return DASH if v is None else f"{v:.{nd}f}%"


def _num(v, suffix="") -> str:
    return DASH if v is None else f"{v:g}{suffix}"


def _money(v) -> str:
    if v is None:
        return DASH
    v = float(v)
    return f"{'+' if v > 0 else '−' if v < 0 else ''}₹{abs(v):,.2f}"


def _name(sym: str) -> str:
    """SBIN-EQ -> SBIN on the page: every screened instrument is a cash share, and the suffix only costs width on a phone."""
    return sym[:-3] if sym.endswith("-EQ") else sym


def _tags(r: dict) -> str:
    return "".join(f'<span class="tag">{esc(t)}</span>' for t in r.get("indices") or [])


def _day(iso: str | None) -> str:
    try:
        d = datetime.fromisoformat(str(iso))
        return f"{d.day} {MONTHS[d.month - 1]}"
    except (TypeError, ValueError):
        return "unknown date"


def lists_text(snap: dict) -> str:
    """Which lists were screened, e.g. 'Nifty 50 (50, list from 22 Oct) + Bank Nifty (14, list from 22 Oct)'. Plain text."""
    lists = snap.get("lists") or []
    if not lists:
        return "a hand-picked list of stocks"
    parts = []
    for x in lists:
        old = f", {x['age_days']} days old: refresh it" if x.get("stale") else ""
        parts.append(f"{x['title']} ({x['count']}, list from {_day(x.get('fetched_at'))}{old})")
    return " + ".join(parts)


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
    qty, cap = int(p.get("qty") or 0), float(p.get("max_order_value") or 0)
    amt = float(p.get("amount") or 0)
    if amt:
        size = f"the shares that fit in your ₹{amt:,.0f} per trade"
    elif qty:
        size = f"{qty} share{'s' if qty != 1 else ''} per order, the bot's order size"
    else:
        size = f"an order of about ₹{float(p['order_value']):,.0f}"
    if amt:
        cap_gate = f", at least one share fitting in ₹{amt:,.0f}"
    else:
        cap_gate = f", {'the order' if qty else 'one order'} costing at most the ₹{cap:,.0f} order cap" if cap else ""
    return [
        f"A stock is only scored if it passes the gates: at least ₹{float(p['min_traded_value_cr']):.0f} crore traded "
        f"per day on average, a spread of at most {float(p['max_spread_pct']):.2f}% of the price, an average daily range "
        f"at least {float(p['min_range_to_cost']):.0f} times the round-trip fees{cap_gate}, and {p['min_history_days']} days of history.",
        f"Fees are worked out for {size}, since fees as a share of the order depend on its size.",
        f"The score (0 to 100) ranks each stock against the others on room to move versus fees ({share('range_to_cost')}), "
        f"the size of today's gap ({share('gap')}), how much of a normal day's volume has traded so far ({share('volume')}) "
        f"and liquidity ({share('liquidity')}). A factor with no data is left out for that stock, not guessed.",
        "Volume traded so far is only comparable between stocks at the same moment, and means more later in the morning.",
    ] + ([f"Strategy per stock: each night every strategy is backtested on each stock. A stock's strategy is the one "
          "with the best result after fees on the older two-thirds of the days, and it is kept only if it also made money "
          "on the newer third, which it was not chosen on. A stock where no strategy passes is left out. This is a coarse "
          "filter on a few weeks of history, not proof a stock will make money."] if p.get("strategy_per_stock") else []) + \
        ([f"Backtest check: each stock was backtested the evening before with today's strategy and order size; a stock "
          f"whose backtest lost money after fees, or made fewer than {p['bt_min_trades']} trades, is left out. "
          "A few weeks of history is a coarse filter, not proof the stock will make money."]
         if p.get("backtest_gate") and not p.get("strategy_per_stock") else [])


def settings_changed(snap: dict, cfg: dict | None) -> str | None:
    """A note when this screen ran with other settings than today's (picks count or the lists screened)."""
    if not cfg:
        return None
    from bot import universe as U
    from bot.screener import params_from
    now_n = int(params_from(cfg)["top_n"])
    then_n = int((snap.get("params") or {}).get("top_n") or now_n)
    entries = list((cfg.get("screener") or {}).get("universe") or [])
    now_lists = list(dict.fromkeys(filter(None, (U.canonical(str(e)) for e in entries))))   # config order, no repeats
    then_lists = [x["name"] for x in snap.get("lists") or []]
    diffs = []
    if now_n != then_n:
        diffs.append(f"{then_n} picks, now {now_n}")
    if sorted(now_lists) != sorted(then_lists):
        diffs.append(f"{lists_text(snap)}, now {' + '.join(U.PRESETS[n]['title'] for n in now_lists) or 'a hand-picked list'}")
    if not diffs:
        return None
    return ("This screen ran with earlier settings (" + "; ".join(diffs) + "). The next screen uses your current "
            "settings: the next trading morning at 09:16, or now with  ./deploy/ab paper screen  on the VM.")


def decision_text(snap: dict, decision: dict | None) -> str | None:
    """What the bot decided this morning, if the decision is for this snapshot's day."""
    d = decision or {}
    if not d or d.get("market_date") != snap.get("market_date"):
        return None
    when = str(d.get("decided_at", ""))[11:16]
    syms = d.get("symbols") or []
    if not syms:
        return f"No trading today (decided {when}): {d.get('note') or 'no stock passed the checks'}."
    per = d.get("strategies") or {}
    names = ", ".join(f"{_name(s)} ({per[s]})" if per.get(s) else _name(s) for s in syms)
    who = {"auto": "top picks", "you": "your choice", "timeout": "no tap", "config": "your configured list"}.get(d.get("source"), d.get("source") or "")
    return f"Trading today (decided {when}, {who}): {names}."


# the first gate a stock failed, as a short group name (for the "why nothing passed" summary)
REASON_GROUPS = [("only ", "Too little history"), ("strategy check", "No strategy passed the check"),
                 ("no strategy passed", "No strategy passed the check"), ("backtest", "Backtest check failed"),
                 ("one share costs", "One share costs more than the amount"), ("cost ₹", "Order above the cap"),
                 ("traded value", "Too little traded"), ("spread", "Spread too wide"), ("daily range", "Too little room vs fees")]


def reason_group(reason: str) -> str:
    for prefix, name in REASON_GROUPS:
        if reason.startswith(prefix) or (prefix == "cost ₹" and prefix in reason):
            return name
    return "Other"


def why_left_out(bad: list[dict]) -> list[tuple[str, int]]:
    """[(group, stocks)], most first, counting each stock once by the first gate it failed."""
    count: dict[str, int] = {}
    for r in bad:
        g = reason_group((r.get("excluded") or ["other"])[0])
        count[g] = count.get(g, 0) + 1
    return sorted(count.items(), key=lambda kv: -kv[1])


def render_html(snap: dict, decision: dict | None = None, cfg: dict | None = None) -> str:
    p = snap["params"]
    ok = [r for r in snap["rows"] if not r["excluded"]]
    bad = [r for r in snap["rows"] if r["excluded"]]
    picks = ok[: int(p["top_n"])]

    def size_chip(r):
        if p.get("qty"):                            # fixed shares: show what one order costs
            v = r.get("order_value")
            return f'<span>{r["suggested_qty"]} shares<b>{esc(DASH if v is None else f"₹{v:,.0f}")}</b></span>'
        return f'<span>Qty for ₹{float(p["order_value"]):,.0f}<b>{esc(_num(r["suggested_qty"]))}</b></span>'

    def bt_chip(r):
        ch = r.get("choice") or {}
        if ch.get("strategy"):
            t = ch.get("test") or {}
            return (f'<span>Strategy<b>{esc(ch["strategy"])}</b></span>'
                    f'<span>Checked on newer days<b>{esc(_money(t.get("net_pnl")))} · {esc(str(t.get("trades")))} trades</b></span>')
        bt = r.get("bt")
        if not bt:
            return ""
        return (f'<span>Backtest {esc(str(bt.get("days")))}d<b>{esc(_money(bt.get("net_pnl")))} · '
                f'{esc(str(bt.get("trades")))} trades</b></span>')

    cards = "".join(
        f'<div class="card pick"><div class="rk">#{r["rank"]}</div><div class="sym">{esc(_name(r["symbol"]))}{_tags(r)}</div>'
        f'<div class="sc"><b>{r["score"]:.0f}</b> / 100</div><div class="bar"><div style="width:{r["score"]:.0f}%"></div></div>'
        f'<ul>{"".join("<li>" + esc(w) + "</li>" for w in r["why"])}</ul>'
        f'<div class="chips"><span>Price<b>{esc(_num(r["ltp"], ""))}</b></span><span>Gap<b>{esc(_gap(r["gap_pct"]))}</b></span>'
        f'<span>Room vs fees<b>{esc(_num(r["range_to_cost"], "x"))}</b></span><span>Spread<b>{esc(_pct(r["spread_pct"], 2))}</b></span>'
        f'{size_chip(r)}'
        f'<span>Traded/day<b>{esc(_num(r["traded_value_cr"], " cr"))}</b></span>{bt_chip(r)}</div></div>' for r in picks)
    if not picks:
        groups = why_left_out(bad)
        top = max([n for _, n in groups] or [1])
        rows_ = "".join(f'<div class="why"><span>{esc(g)}</span><b>{n}</b><div class="bar"><div style="width:{n / top * 100:.0f}%"></div></div></div>'
                        for g, n in groups)
        cards = ('<div class="card"><h2>No stock passed the gates today</h2>'
                 f'<p class="note">Each stock is counted once, by the first check it failed. Details are under Left out.</p>'
                 f'<div class="whys">{rows_}</div></div>')

    def gap_cell(v):
        cls = "" if v is None else ("up" if v > 0 else "dn" if v < 0 else "")
        return f'<td class="r {cls}">{esc(_gap(v))}</td>'

    def table(rows_: list[dict]) -> str:
        if not rows_:
            return '<p class="note">No stock from this list passed the gates.</p>'
        body = "".join(
            f'<tr><td>{r["rank"]}</td><td>{esc(_name(r["symbol"]))}{_tags(r)}</td><td class="r">{r["score"]:.0f}</td>{gap_cell(r["gap_pct"])}'
            f'<td class="r">{esc(_num(r["range_to_cost"], "x"))}</td><td class="r">{esc(_pct(r["vol_pct_of_avg"], 0))}</td>'
            f'<td class="r hide-s">{esc(_pct(r["spread_pct"], 2))}</td><td class="r hide-s">{esc(_num(r["traded_value_cr"]))}</td></tr>'
            for r in rows_)
        return ('<div class="scroll"><table><thead><tr><th>#</th><th>Stock</th><th class="r">Score</th><th class="r">Gap</th>'
                '<th class="r">Room vs fees</th><th class="r">Volume</th><th class="r hide-s">Spread</th><th class="r hide-s">Traded cr</th>'
                f'</tr></thead><tbody>{body}</tbody></table></div>')

    # Tabs without JavaScript (a hidden radio button per tab; the label is the button), so they work in phone previews too
    tabs = [] if not ok else [("all", f"All ({len(ok)})", "Every stock that passed the gates, best first. Gap is today's open against yesterday's "
             "close. Volume is the share of a normal day already traded. Traded is \u20b9 crore a day on average", table(ok))]
    for x in (snap.get("lists") or []) if ok else []:
        sub = [r for r in ok if x["tag"] in (r.get("indices") or [])]
        tabs.append((x["tag"].lower(), f"{x['title']} ({len(sub)})", f"{x['title']} stocks that passed the gates, best first", table(sub)))
    if bad:
        groups: dict[str, list[dict]] = {}
        for r in bad:
            groups.setdefault(reason_group((r["excluded"] or ["other"])[0]), []).append(r)
        def detail(r):                                     # the group heading already says which check it was
            first = r["excluded"][0]
            for pre in ("no strategy passed the check: ", "strategy check: ", "backtest check: "):
                if first.startswith(pre):
                    first = first[len(pre):]
            return "; ".join([first] + r["excluded"][1:])
        body_ = "".join(f'<h3 class="grp">{esc(g)} <span>{len(rs)}</span></h3><ul class="plain tight">'
                        + "".join(f"<li><b>{esc(_name(r['symbol']))}</b>{_tags(r)} <span class=\"why-d\">{esc(detail(r))}</span></li>" for r in rs)
                        + "</ul>" for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])))
        tabs.append(("out", f"Left out ({len(bad)})", "These failed a gate, so they are not scored. Grouped by the first check each failed",
                     body_))
    sc = snap.get("strategy_compare")
    if sc and sc.get("per_stock") and sc.get("walk"):
        tabs.append(("strat", "Strategy check", walk_note(sc), walk_tables(sc)))
    elif sc and sc.get("totals"):
        tabs.append(("strat", "Strategies", strategy_note(sc), strategy_tables(sc)))
    tab_css = "".join(
        f"#st-{k}:checked~.tabs label[for=st-{k}]{{color:#fff;background:var(--accent);border-color:var(--accent)}}"
        f"#st-{k}:focus-visible~.tabs label[for=st-{k}]{{outline:2px solid var(--accent);outline-offset:-2px}}"
        f"#st-{k}:checked~#sp-{k}{{display:block}}" for k, *_ in tabs)
    tabs_html = ('<section class="card">'
                 + "".join(f'<input class="tab-in" type="radio" name="st" id="st-{k}"{" checked" if i == 0 else ""}>' for i, (k, *_) in enumerate(tabs))
                 + '<nav class="tabs" role="tablist">' + "".join(f'<label for="st-{k}" role="tab">{esc(t)}</label>' for k, t, *_ in tabs) + "</nav>"
                 + "".join(f'<div class="panel" id="sp-{k}" role="tabpanel"><p class="note">{esc(n)}</p>{body}</div>' for k, _, n, body in tabs)
                 + '<p class="tabhint">If tapping a tab does nothing, open this file in your phone\'s browser.</p></section>') if tabs else ""
    errs = ""
    if snap.get("errors"):
        items = "".join(f"<li>{esc(e)}</li>" for e in snap["errors"])
        errs = f'<section class="card"><h2>Problems collecting data</h2><p class="note">These stocks or fields are missing from the ranking</p><ul class="plain">{items}</ul></section>'
    how = ('<section class="card"><h2>How the score works</h2><ul class="plain">'
           + "".join(f"<li>{esc(t)}</li>" for t in how_it_works(p)) + "</ul></section>")
    gen = str(snap.get("generated_at", ""))
    decided = decision_text(snap, decision)
    older = settings_changed(snap, cfg)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>AngelBot screener {esc(snap.get("market_date", ""))}</title><style>{CSS}{tab_css}</style></head><body><div class="wrap">'
            f'<header><h1>Stock screener</h1><p class="sub">{esc(date_label(snap))} · built {esc(gen[11:16])} IST</p>'
            f'<div class="kpis"><div class="kpi"><div class="l">Checked</div><div class="v">{snap["universe"]}</div></div>'
            f'<div class="kpi"><div class="l">Passed the gates</div><div class="v">{snap["eligible"]}</div></div></div>'
            f'<p class="sub">Screened: {esc(lists_text(snap))}</p></header>'
            + (f'<div class="banner decided"><i></i><span><b>{esc(decided)}</b></span></div>' if decided else "")
            + (f'<div class="banner"><i></i><span>{esc(older)}</span></div>' if older else "") +
            f'<section><div class="picks">{cards}</div></section>{tabs_html}{errs}{how}'
            f'<p class="note">The screener places no orders. With morning selection on, the bot trades the top {int(p["top_n"])} '
            'that passed the checks; the score only orders them and has not been backtested itself.</p>'
            '<footer>Read-only analytics from market data. No orders are placed by the screener.</footer></div></body></html>')


def walk_note(sc: dict) -> str:
    when = str(sc.get("run_at", ""))[:16].replace("T", " ")
    note = (f"The check the bot uses (run {when} IST): each strategy on each stock, on the older two-thirds of the last "
            f"{sc.get('days')} days and, separately, on the newer third. A stock's strategy is the best on the older days, "
            "kept only if it also made money on the newer days. Stocks marked none are left out.")
    if sc.get("note"):
        note += f" Note: {sc['note']}."
    return note


def walk_tables(sc: dict) -> str:
    walk, choices = sc.get("walk") or {}, sc.get("choices") or {}
    passed = sorted(s for s, c in choices.items() if (c or {}).get("strategy"))
    names = sorted({n for w in walk.values() for n in w})
    tot = ""
    for n in names:                                # per strategy, over all stocks
        older = sum(float(((w.get(n) or {}).get("train") or {}).get("net_pnl") or 0) for w in walk.values())
        newer = sum(float(((w.get(n) or {}).get("test") or {}).get("net_pnl") or 0) for w in walk.values())
        chosen = sum(1 for c in choices.values() if (c or {}).get("strategy") == n)
        tot += (f'<tr><td>{esc(n)}</td><td class="r {"up" if older > 0 else "dn" if older < 0 else ""}">{esc(_money(older))}</td>'
                f'<td class="r {"up" if newer > 0 else "dn" if newer < 0 else ""}">{esc(_money(newer))}</td><td class="r">{chosen}</td></tr>')
    order = sorted(walk, key=lambda s: (not (choices.get(s) or {}).get("strategy"), s))
    items = ""
    for sym in order:
        c = choices.get(sym) or {}
        head = (f'<b>{esc(_name(sym))}</b> → <b class="up">{esc(c["strategy"])}</b>' if c.get("strategy")
                else f'<b>{esc(_name(sym))}</b> → none <span class="note">({esc(c.get("why") or "not checked")})</span>')
        parts = []
        for n in names:
            w = walk[sym].get(n) or {}
            tr, te = w.get("train") or {}, w.get("test") or {}
            parts.append(f'{esc(n)} {esc(_money(tr.get("net_pnl")))} / {esc(_money(te.get("net_pnl")))}')
        items += f'<li>{head}<br><span class="note">older / newer: {" · ".join(parts)}</span></li>'
    return (f'<p class="note">{len(passed)} of {len(walk)} stocks passed: {esc(", ".join(_name(s) for s in passed) or "none")}</p>'
            '<div class="scroll"><table><thead><tr><th>Strategy</th><th class="r">Older days</th><th class="r">Newer days</th>'
            f'<th class="r">Chosen for</th></tr></thead><tbody>{tot}</tbody></table></div>'
            f'<ul class="plain">{items}</ul>')


def strategy_note(sc: dict) -> str:
    when = str(sc.get("run_at", ""))[:16].replace("T", " ")
    note = (f"Each strategy backtested on every stock over the last {sc.get('days')} days (run {when} IST), same order "
            f"size, exits and limits. Information only: the bot keeps using {sc['current']} until you change it in "
            "Settings. Last month's best need not be next month's: confirm on a later period before switching.")
    if sc.get("note"):
        note += f" Note: {sc['note']}."
    return note


def strategy_tables(sc: dict) -> str:
    names = [t["strategy"] for t in sc["totals"]]
    cur = sc["current"]
    tot = "".join(
        f'<tr><td>{esc(t["strategy"])}{" <b>(in use)</b>" if t["strategy"] == cur else ""}</td>'
        f'<td class="r {"up" if t["net_pnl"] > 0 else "dn" if t["net_pnl"] < 0 else ""}">{esc(_money(t["net_pnl"]))}</td>'
        f'<td class="r">{t["trades"]}</td><td class="r">{t["win_rate_pct"]:g}%</td>'
        f'<td class="r">{t["stocks_up"]} of {t["stocks"]}</td><td class="r hide-s">{esc(_money(-t["charges"]) if t["charges"] else DASH)}</td></tr>'
        for t in sc["totals"])
    head = "".join(f'<th class="r">{esc(n)}</th>' for n in names)
    body = ""
    for sym, per in sorted(sc["by_stock"].items(), key=lambda kv: -float((kv[1].get(cur) or {}).get("net_pnl") or 0)):
        cells = ""
        for n in names:
            r = per.get(n) or {}
            v = r.get("net_pnl")
            cls = "" if v is None else ("up" if v > 0 else "dn" if v < 0 else "")
            cells += f'<td class="r {cls}">{esc(_money(v))}<br><span class="note">{esc(str(r.get("trades", DASH)))} trades</span></td>'
        body += f"<tr><td>{esc(_name(sym))}</td>{cells}</tr>"
    return ('<div class="scroll"><table><thead><tr><th>Strategy</th><th class="r">Net after fees</th><th class="r">Trades</th>'
            '<th class="r">Win rate</th><th class="r">Stocks up</th><th class="r hide-s">Fees</th></tr></thead>'
            f'<tbody>{tot}</tbody></table></div>'
            '<p class="note">Per stock (net after fees, and trades)</p>'
            f'<div class="scroll"><table><thead><tr><th>Stock</th>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def summary_html(snap: dict, today: str | None = None, decision: dict | None = None, cfg: dict | None = None) -> str:
    """Short Telegram message (HTML) with the top picks."""
    ok = [r for r in snap["rows"] if not r["excluded"]]
    n = int(snap["params"]["top_n"])
    lines = [f"\U0001F50E <b>Stock screener</b> · {esc(date_label(snap))} {esc(str(snap.get('generated_at', ''))[11:16])} IST"]
    if today and snap.get("market_date") != today:
        lines.append(f"⚠️ This snapshot is from {esc(snap.get('market_date'))}, not today.")
    lines.append(f"Screened: {esc(lists_text(snap))}")
    decided = decision_text(snap, decision)
    if decided:
        lines.append(f"<b>{esc(decided)}</b>")
    older = settings_changed(snap, cfg)
    if older:
        lines.append(f"⚠️ {esc(older)}")
    if not ok:
        lines.append("No stock passed the gates.")
    for r in ok[:n]:
        bits = [f"score {r['score']:.0f}", f"gap {_gap(r['gap_pct'])}", f"room {_num(r['range_to_cost'], 'x')} fees"]
        if r.get("strategy"):
            bits.append(f"strategy {r['strategy']}")
        elif r.get("bt"):
            bits.append(f"backtest {_money(r['bt'].get('net_pnl'))} in {r['bt'].get('trades')} trades")
        tags = "".join(f" [{esc(t)}]" for t in r.get("indices") or [])
        lines.append(f"{r['rank']}. <b>{esc(r['symbol'])}</b>{tags} · " + " · ".join(bits))
    lines.append(f"Checked {snap['universe']} stocks, {snap['eligible']} passed the gates.")
    sc = snap.get("strategy_compare")
    if sc and sc.get("per_stock") and sc.get("walk"):
        ch = sc.get("choices") or {}
        passed = sorted(s for s, c in ch.items() if (c or {}).get("strategy"))
        when = str(sc.get("run_at", ""))[5:16].replace("T", " ")
        listed = ", ".join(f"{_name(s)} {ch[s]['strategy']}" for s in passed)
        lines.append(f"Strategy check (run {esc(when)}): {len(passed)} of {len(sc['walk'])} stocks passed"
                     + (f": {esc(listed)}" if passed else "."))
        if sc.get("note"):
            lines.append(f"  ⚠️ {esc(sc['note'])}")
    elif sc and sc.get("totals"):
        lines.append(f"Strategies, last {sc.get('days')} days on these stocks (information only, in use: {esc(sc['current'])}):")
        for t in sc["totals"]:
            mark = " ◀ in use" if t["strategy"] == sc["current"] else ""
            lines.append(f"  {esc(t['strategy'])}: {esc(_money(t['net_pnl']))} · {t['trades']} trades · "
                         f"{t['win_rate_pct']:g}% wins{mark}")
        if sc.get("note"):
            lines.append(f"  ⚠️ {esc(sc['note'])}")
    lines.append(f"The screener places no orders. With morning selection on, the bot trades the top {n} that passed the checks.")
    return "\n".join(lines)
