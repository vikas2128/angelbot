"""Bar-based backtester using the same strategies, exits and risk rules as the live engine.

Fill model (deliberately conservative):
  * signal on bar close → fill at NEXT bar open ± slippage
  * stop checked before target inside a bar (worst case); gaps fill at the open
  * square-off at the first bar at/after session.squareoff
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from bot.charges import order_charges
from bot.data import resample, split_days
from bot import sizing
from bot.models import Bar
from bot.strategies import build
from bot.timeutil import at


@dataclass
class BTTrade:
    day: str
    side: str
    qty: int
    entry_ts: str
    entry: float
    exit_ts: str
    exit: float
    reason: str
    gross: float
    charges: float
    net: float


def run_backtest(cfg: dict, bars_1m: list[Bar], qty: int = 1, strategy: str | None = None,
                 params: dict | None = None, out_csv: Path | None = None) -> dict:
    st = cfg["strategy"]
    name = strategy or st["name"]
    p = params if params is not None else (st.get("params") or {}).get(name, {})
    tf = int(st.get("timeframe_min", 1))
    allow_short = bool(st.get("allow_short"))
    ex, rk, ss = cfg["exits"], cfg["risk"], cfg["session"]
    slip = float(cfg.get("paper", {}).get("slippage_pct", 0.02)) / 100
    sl_pct, tgt_pct = float(ex["stop_loss_pct"]) / 100, float(ex.get("target_pct") or 0) / 100
    trail = float(ex.get("trailing_sl_pct") or 0) / 100
    charges_cfg = cfg.get("charges", {})
    amount = sizing.amount(cfg)         # > 0: shares at each entry = what fits in this many rupees

    trades: list[BTTrade] = []
    equity, peak, max_dd = 0.0, 0.0, 0.0
    daily: dict[str, float] = {}

    for day, day_bars in sorted(split_days(resample(bars_1m, tf)).items()):
        strat = build(name, p)
        strat.reset_day()
        pos, entry_px, entry_ts, stop, target, best = 0, 0.0, None, None, None, None
        pos_qty = qty
        pending: int | None = None          # desired side to execute at next open
        trades_today, day_pnl, consec, cooldown_until, halted = 0, 0.0, 0, None, False
        seen: list[Bar] = []

        def close(bar_ts: datetime, px: float, reason: str):
            nonlocal pos, pos_qty, day_pnl, equity, peak, max_dd, consec, cooldown_until, halted
            q = pos_qty
            gross = (px - entry_px) * q * (1 if pos > 0 else -1)
            ch = order_charges("BUY" if pos > 0 else "SELL", q, entry_px, charges_cfg) + \
                 order_charges("SELL" if pos > 0 else "BUY", q, px, charges_cfg)
            net = gross - ch
            trades.append(BTTrade(day, "LONG" if pos > 0 else "SHORT", q, entry_ts.isoformat(), round(entry_px, 2),
                                  bar_ts.isoformat(), round(px, 2), reason, round(gross, 2), round(ch, 2), round(net, 2)))
            day_pnl += net
            equity += net
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
            consec = consec + 1 if net < 0 else 0
            if consec >= int(rk["max_consecutive_losses"]):
                from datetime import timedelta
                cooldown_until = bar_ts + timedelta(minutes=int(rk["cooldown_minutes"]))
                consec = 0
            if day_pnl <= -abs(float(rk["max_daily_loss"])):
                halted = True
            if float(rk.get("max_daily_profit") or 0) > 0 and day_pnl >= float(rk["max_daily_profit"]):
                halted = True
            pos = 0

        for bar in day_bars:
            # 1) execute pending order at this bar's open
            if pending is not None:
                desired, pending = pending, None
                if pos != 0 and desired != pos:
                    close(bar.start, bar.open * (1 - slip if pos > 0 else 1 + slip), "signal")
                fill_px = bar.open * (1 + slip if desired > 0 else 1 - slip)
                eq = sizing.qty_for(amount, max(bar.open, fill_px)) if amount else qty
                can_enter = (desired != 0 and pos == 0 and not halted and eq > 0
                             and at(bar.start, ss["entry_start"]) <= bar.start < at(bar.start, ss["entry_cutoff"])
                             and trades_today < int(rk["max_trades_per_day"])
                             and not (cooldown_until and bar.start < cooldown_until)
                             and bar.open * eq <= float(rk["max_order_value"]))
                if can_enter:
                    pos, pos_qty = desired, eq
                    entry_px = bar.open * (1 + slip if pos > 0 else 1 - slip)
                    entry_ts = bar.start
                    stop = entry_px * (1 - sl_pct) if pos > 0 else entry_px * (1 + sl_pct)
                    target = (entry_px * (1 + tgt_pct) if pos > 0 else entry_px * (1 - tgt_pct)) if tgt_pct else None
                    best = entry_px
                    trades_today += 1

            # 2) square-off
            if pos != 0 and bar.start >= at(bar.start, ss["squareoff"]):
                close(bar.start, bar.open * (1 - slip if pos > 0 else 1 + slip), "squareoff")

            # 3) intrabar stop / target (stop first = pessimistic)
            if pos > 0:
                if bar.low <= stop:
                    close(bar.start, min(stop, bar.open), "stop_loss")
                elif target and bar.high >= target:
                    close(bar.start, max(target, bar.open), "target")
                elif trail:
                    best = max(best, bar.high); stop = max(stop, best * (1 - trail))
            elif pos < 0:
                if bar.high >= stop:
                    close(bar.start, max(stop, bar.open), "stop_loss")
                elif target and bar.low <= target:
                    close(bar.start, min(target, bar.open), "target")
                elif trail:
                    best = min(best, bar.low); stop = min(stop, best * (1 + trail))

            # 4) strategy on bar close
            seen.append(bar)
            d = strat.on_bar(seen, (pos > 0) - (pos < 0))
            if d is not None:
                if d < 0 and not allow_short:
                    d = 0
                if d != ((pos > 0) - (pos < 0)):
                    pending = d

        if pos != 0:  # end of data for the day
            last = day_bars[-1]
            close(last.start, last.close, "end_of_day")
        daily[day] = round(day_pnl, 2)

    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    gross_win, gross_loss = sum(t.net for t in wins), -sum(t.net for t in losses)
    summary = {
        "strategy": name, "params": p, "timeframe_min": tf, "qty": qty, "amount_per_trade": amount or None,
        "days": len(daily), "trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "net_pnl": round(sum(t.net for t in trades), 2),
        "gross_pnl": round(sum(t.gross for t in trades), 2),
        "charges": round(sum(t.charges for t in trades), 2),
        "avg_trade": round(sum(t.net for t in trades) / len(trades), 2) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "net_won": round(gross_win, 2), "net_lost": round(gross_loss, 2),     # so results can be pooled across runs
        "wins": len(wins),
        "max_drawdown": round(max_dd, 2),
        "green_days": sum(1 for v in daily.values() if v > 0),
        "red_days": sum(1 for v in daily.values() if v < 0),
        "exit_reasons": {r: sum(1 for t in trades if t.reason == r) for r in {t.reason for t in trades}},
    }
    if out_csv:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()) if trades else ["day"])
            w.writeheader()
            for t in trades:
                w.writerow(asdict(t))
        summary["trades_csv"] = str(out_csv)
    summary["daily_pnl"] = daily
    return summary
