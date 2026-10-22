"""Plain-English Telegram message cards (HTML). Pure functions: no I/O, easy to test.

Everything dynamic goes through esc() because Telegram's HTML mode rejects stray < > &.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from html import escape as esc

EXIT_REASONS = {
    "stop_loss": "Safety exit hit (loss kept small)",
    "target": "Goal reached",
    "signal": "Strategy said to exit",
    "squareoff time": "Market-close exit",
    "KILL SWITCH": "Emergency stop",
    "end of replay data": "Replay finished",
}
SHORT_REASONS = {
    "stop_loss": "Safety exit",
    "target": "Goal reached",
    "signal": "Strategy exit",
    "squareoff time": "Market close",
    "KILL SWITCH": "Emergency stop",
}
STRATEGIES = {
    "ema_cross": "trend crossover",
    "orb": "opening range breakout",
    "vwap_revert": "price far from daily average",
}


# ───────────────────────────── small helpers ─────────────────────────────
def money(x) -> str:
    """Signed rupees: +₹14.20 / -₹3.10."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "–"
    return f"{'+' if v >= 0 else '-'}₹{abs(v):,.2f}"


def price(x) -> str:
    try:
        return f"₹{float(x):,.2f}"
    except (TypeError, ValueError):
        return "–"


def shares(n) -> str:
    n = abs(int(n))
    return f"{n} share" if n == 1 else f"{n} shares"


def mode_tag(mode) -> str:
    if mode == "live":
        return "🔴 <b>LIVE</b> (real money)"
    return "🧪 <b>TEST</b> (paper trading, no real money)"


def exit_reason(reason: str | None) -> str:
    return EXIT_REASONS.get(reason or "", esc(reason or "Exit"))


def halt_reason(reason: str | None) -> str:
    r = reason or ""
    if r.startswith("max_daily_loss"):
        return "Daily loss limit reached"
    if r.startswith("max_daily_profit"):
        return "Daily profit goal reached (locking it in)"
    return esc(r)


def strategy_name(name: str | None) -> str:
    return STRATEGIES.get(name or "", esc(name or "strategy"))


def stamp(ts) -> str:
    """Trade time as 21 Oct, 10:05:32 IST (empty if unknown)."""
    try:
        return ts.strftime("%d %b, %H:%M:%S") + " IST"
    except AttributeError:
        return ""


def _day(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return esc(day)


# ───────────────────────────── alerts from the bot ─────────────────────────────
def started(mode, strategy, symbols) -> str:
    return (f"▶️ <b>AngelBot started</b>\n{mode_tag(mode)}\n"
            f"Looking for: {strategy_name(strategy)}\n"
            f"Watching: {esc(', '.join(symbols))}")


def opened(symbol, side, qty, px, stop, target, strategy, mode, ts=None) -> str:
    verb = "Bought" if side == "LONG" else "Sold (short)"
    lines = [f"🟢 <b>{verb} {esc(symbol)}</b>",
             f"{shares(qty)} at {price(px)}"]
    if stamp(ts):
        lines.append(f"🕐 {stamp(ts)}")
    exits = []
    if stop:
        exits.append(f"Safety exit: {price(stop)}")
    if target:
        exits.append(f"Goal: {price(target)}")
    if exits:
        lines.append("   ".join(exits))
    lines.append(f"Why: {strategy_name(strategy)} signal")
    return "\n".join(lines)


def closed(symbol, side, qty, entry, exit_px, gross, charges, net, reason, mode,
           entry_ts=None, exit_ts=None) -> str:
    won = net > 0
    head = "✅ <b>Closed {} with a profit</b>" if won else "🔻 <b>Closed {} with a loss</b>"
    if net == 0:
        head = "➖ <b>Closed {} at break-even</b>"
    lines = [head.format(esc(symbol)),
             f"<b>{money(net)}</b> after all fees",
             f"{'Bought' if side == 'LONG' else 'Sold'} {price(entry)} → "
             f"{'Sold' if side == 'LONG' else 'Bought back'} {price(exit_px)} · {shares(qty)}",
             f"Fees: {price(charges)}",
             f"Reason: {exit_reason(reason)}"]
    if stamp(entry_ts) and stamp(exit_ts):
        lines.append(f"🕐 In {stamp(entry_ts)}\n🕐 Out {stamp(exit_ts)}")
    elif stamp(exit_ts):
        lines.append(f"🕐 {stamp(exit_ts)}")
    return "\n".join(lines)


def flatten(reason: str) -> str:
    text = {"KILL SWITCH": "Emergency stop pressed", "squareoff time": "Market-close time"}.get(
        reason, halt_reason(reason))
    return f"⛔ <b>Closing all positions</b>\nReason: {text}"


def adopted(symbol, qty, avg) -> str:
    return (f"♻️ <b>Picked up an open position</b>\n{esc(symbol)}: {shares(qty)} at {price(avg)}\n"
            "It was already open (bot restarted), so I'm managing it now.")


def exit_stuck(symbol, chases) -> str:
    return (f"⚠️ <b>Exit not filling: {esc(symbol)}</b>\n"
            f"Tried {chases} times to sell/buy back. Please check the Angel One app now.")


def order_problem(side, qty, symbol, err) -> str:
    return f"❗ <b>Order failed</b>\n{esc(side)} {shares(qty)} {esc(symbol)}\nBroker said: {esc(str(err))}"


def order_rejected(purpose, side, symbol, reason) -> str:
    what = "Entry" if purpose == "ENTRY" else "Exit"
    return f"❗ <b>{what} order rejected</b>\n{esc(side)} {esc(symbol)}\nBroker said: {esc(reason or 'no reason given')}"


def feed_trouble(last_tick, restarts) -> str:
    seen = f"Last price seen at {esc(last_tick)}" if last_tick else "No price received yet"
    return (f"📡 <b>Price feed problem</b>\n{seen}. Reconnecting (restart #{restarts}).\n"
            "No new trades can be judged until prices flow again; open positions still have their safety exit.")


def feed_back() -> str:
    return "📡 <b>Price feed is back</b>\nPrices are flowing again."


def cooldown(until, losses) -> str:
    return (f"⏳ <b>Taking a break</b>\n{losses} losing trades in a row.\n"
            f"No new trades until {esc(until.strftime('%H:%M'))}. Open positions stay protected.")


def heartbeat(feed_ok, prices: dict, paused: bool, max_loss) -> str:
    lines = ["☀️ <b>Good morning: bot is ready</b>",
             f"Price feed: {'connected ✅' if feed_ok else 'NOT connected ⚠️'}"]
    for sym, px in prices.items():
        lines.append(f"{esc(sym)}: {price(px)}")
    if not prices:
        lines.append("No prices yet")
    lines.append("New trades are paused ⏸" if paused else "New trades are allowed from now")
    lines.append(f"Daily loss limit: {price(max_loss)}")
    return "\n".join(lines)


def shutdown_open() -> str:
    return "🚨 <b>Bot stopped but a position or order is still open</b>\nCheck the Angel One app now."


# ───────────────────────────── summaries ─────────────────────────────
def daily_summary(rows: list[dict], day: str, mode=None, show_trades: bool = True,
                  title: str = "Today's summary") -> str:
    head = f"📊 <b>{esc(title)}</b> · {_day(day)}"
    if mode:
        head += f"\n{mode_tag(mode)}"
    if not rows:
        return head + "\nNo trades closed yet."
    nets = [float(r.get("net_pnl") or 0) for r in rows]
    net = sum(nets)
    won = sum(1 for n in nets if n > 0)
    lost = sum(1 for n in nets if n < 0)
    fees = sum(float(r.get("charges") or 0) for r in rows)
    icon = "✅" if net > 0 else "🔻" if net < 0 else "➖"
    best = max(rows, key=lambda r: r.get("net_pnl") or 0)
    worst = min(rows, key=lambda r: r.get("net_pnl") or 0)
    why = Counter(SHORT_REASONS.get(r.get("exit_reason"), r.get("exit_reason") or "Other") for r in rows)
    lines = [head, f"{icon} <b>Net result: {money(net)}</b> (after fees)",
             f"Trades: {len(rows)} · Won {won} · Lost {lost}",
             f"Best: {esc(best['symbol'])} {money(best.get('net_pnl'))}"
             f" · Worst: {esc(worst['symbol'])} {money(worst.get('net_pnl'))}",
             f"Fees paid: {price(fees)}",
             "How trades ended: " + ", ".join(f"{esc(k)} {v}" for k, v in why.most_common())]
    if show_trades:
        lines.append("")
        for r in rows[-10:]:
            t = (r.get("exit_ts") or "")[11:16]
            lines.append(f"{t} {esc(r['symbol'])} {money(r.get('net_pnl'))}")
    if mode != "live":
        lines.append("\n🧪 Test results only. Small samples prove nothing.")
    return "\n".join(lines)


def status_card(s: dict, control: dict, configured_mode: str | None = None) -> str:
    if not s.get("written_at"):
        return (f"⚪ <b>Bot hasn't run yet</b>\nMode set to: {mode_tag(configured_mode)}\n"
                f"Entries paused: {'yes' if control.get('paused') else 'no'}")
    alive = bool(s.get("bot_alive"))
    head = "🟢 <b>Bot is running</b>" if alive else \
        f"⚪ <b>Bot is not running</b> (last update {esc(str(s.get('age_sec', '?')))}s ago)"
    if s.get("replay"):
        head += " (replay)"
    lines = [head, mode_tag(s.get("mode")),
             f"Looking for: {strategy_name(str(s.get('strategy') or '').split('(')[0])}",
             f"Today: <b>{money(s.get('day_pnl'))}</b> after fees · {s.get('trades_today', 0)} trades"]
    positions = s.get("positions") or []
    if positions:
        for p in positions:
            lines.append(f"📌 Holding {esc(p['symbol'])}: {shares(p['qty'])} bought at {price(p['avg'])}")
            lines.append(f"   Now {price(p.get('ltp'))} · so far {money(p.get('unrealized'))}")
            lines.append(f"   Safety exit {price(p.get('stop'))} · Goal {price(p.get('target'))}")
    else:
        lines.append("No open positions")
    if s.get("pending_orders"):
        lines.append(f"Orders waiting: {len(s['pending_orders'])}")
    if s.get("halted_reason"):
        lines.append(f"⛔ Stopped for today: {halt_reason(s['halted_reason'])}")
    if s.get("cooldown_until"):
        lines.append(f"⏳ Resting after losses until {esc(s['cooldown_until'][11:16])}")
    feed = s.get("feed") or {}
    if alive and not s.get("replay") and feed.get("connected") is False:
        lines.append("⚠️ Price feed is disconnected")
    if control.get("paused"):
        lines.append("⏸ New entries are paused")
    if control.get("kill"):
        lines.append("⛔ Emergency stop is set")
    return "\n".join(lines)


# ───────────────────────────── morning stock selection ─────────────────────────────
def selection_problem(err: str) -> str:
    return f"\u26a0\ufe0f <b>Stock selection settings problem</b>\n{esc(err)}\nUsing your configured list today."


def picks_prompt(rows: list[dict], top: list[str], deadline: str, default: list[str]) -> str:
    lines = ["\U0001F50E <b>Today's picks</b> (advisory ranking, not backtested)"]
    for i, r in enumerate(rows, 1):
        gap = "n/a" if r.get("gap_pct") is None else f"{r['gap_pct']:+.1f}%"
        room = "n/a" if r.get("range_to_cost") is None else f"{r['range_to_cost']:.1f}x"
        lines.append(f"{i}. <b>{esc(r['symbol'])}</b> \u00b7 score {r['score']:.0f} \u00b7 gap {gap} \u00b7 room {room} fees")
    lines.append(f"\nTap what the bot should trade today. If you don't, by {esc(deadline)} it uses: "
                 f"<b>{esc(', '.join(default) or 'nothing')}</b>.")
    lines.append("Trades still follow the strategy, stops and risk limits.")
    return "\n".join(lines)


def picks_buttons(shown: list[str], top: list[str], configured: list[str]) -> list[list[dict]]:
    rows = [[{"text": f"\u25b6 Only {s}", "callback_data": f"sel:{s}"}] for s in shown]
    if len(top) > 1:
        rows.insert(0, [{"text": f"\u2705 Trade top {len(top)}", "callback_data": "sel:top"}])
    rows.append([{"text": f"Keep my list ({', '.join(configured)})", "callback_data": "sel:config"}])
    return rows


def selection_decided(symbols: list[str], source: str, note: str) -> str:
    who = {"auto": "Top picks", "you": "Your choice", "timeout": "No tap", "config": "Your configured list", "none": "No trading"}.get(source, source)
    if not symbols:
        return f"\u26d4 <b>No trading today</b>\n{esc(note)}"
    return f"\u2705 <b>Trading today: {esc(', '.join(symbols))}</b>\n{esc(who)}: {esc(note)}."
