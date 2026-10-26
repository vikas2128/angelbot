"""Research: walk-forward test of a grid of settings across many stocks. Read-only, never changes the config.

For every stock, the trading days in its saved 1-minute candles (data/candles) are split like the nightly check:
the older two-thirds to choose on, the newer third to confirm. Every combination of strategy x bar size x rupees per
trade x stop/target is backtested on both parts with the config's fees, slippage and session times.

Two readings, both judged only on the newer days, which no choice was made on:
- by setting: each combination over all stocks; the list is ordered by the OLDER days' result, so the newer-days
  columns show how a choice made on the past held up;
- per stock: each stock gets the combination that did best on its older days (with enough trades), as the nightly
  check would; the newer-days total of those choices is what that process would have earned.

The daily loss limit is scaled with the rupees per trade (₹500 at ₹10,000 becomes ₹2,500 at ₹50,000); everything
else in `risk` is as configured. Drawdown is from the daily totals of all stocks added together, as if each traded
on its own: a portfolio holding at most a few positions at once would see less of both profit and drawdown.
"""
from __future__ import annotations

import copy
import itertools
import os
from concurrent.futures import ProcessPoolExecutor

STRATEGY_NAMES = ("ema_cross", "orb", "vwap_revert")
TIMEFRAMES = (5, 15)
AMOUNTS = (10000, 25000, 50000)
EXITS = ((0.3, 0.6), (0.5, 1.0), (0.75, 1.5), (1.0, 2.0))        # (stop %, target %)


def combos(strategies=STRATEGY_NAMES, timeframes=TIMEFRAMES, amounts=AMOUNTS, exits=EXITS) -> list[dict]:
    return [{"strategy": s, "tf": tf, "amount": a, "stop": sl, "target": tg}
            for s, tf, a, (sl, tg) in itertools.product(strategies, timeframes, amounts, exits)]


def key(c: dict) -> str:
    return f"{c['strategy']} {c['tf']}m ₹{c['amount'] // 1000}k {c['stop']}/{c['target']}%"


def cfg_for(base: dict, c: dict) -> dict:
    cfg = copy.deepcopy(base)
    cfg["strategy"]["timeframe_min"] = c["tf"]
    cfg.setdefault("sizing", {})["amount"] = c["amount"]
    cfg["exits"] = {**cfg.get("exits", {}), "stop_loss_pct": c["stop"], "target_pct": c["target"]}
    base_amount = float((base.get("sizing") or {}).get("amount") or 0) or 10000.0
    cfg["risk"] = {**cfg["risk"], "max_daily_loss": float(base["risk"]["max_daily_loss"]) * c["amount"] / base_amount,
                   "max_order_value": max(float(cfg["risk"].get("max_order_value") or 0), c["amount"])}
    return cfg


def _part(res: dict) -> dict:
    keep = ("trades", "wins", "net_pnl", "charges", "net_won", "net_lost")
    return {**{k: res.get(k) or 0 for k in keep}, "daily": res.get("daily_pnl") or {}}


def _one_stock(job) -> tuple[str, list]:
    """All combinations for one stock: [(combo index, {"train": part, "test": part}), ...]."""
    from bot.backtest import run_backtest
    base, symbol, train, test, grid = job
    out = []
    for i, c in enumerate(grid):
        cfg = cfg_for(base, c)
        params = (cfg["strategy"].get("params") or {}).get(c["strategy"], {})
        out.append((i, {"train": _part(run_backtest(cfg, train, strategy=c["strategy"], params=params)),
                        "test": _part(run_backtest(cfg, test, strategy=c["strategy"], params=params))}))
    return symbol, out


def pool(parts: list[dict]) -> dict:
    """Add runs together: trades, win rate, net after fees, fees, profit factor and drawdown of the summed days."""
    trades = sum(p["trades"] for p in parts)
    wins = sum(p["wins"] for p in parts)
    won, lost = sum(p["net_won"] for p in parts), sum(p["net_lost"] for p in parts)
    days: dict[str, float] = {}
    for p in parts:
        for d, v in p["daily"].items():
            days[d] = days.get(d, 0.0) + v
    eq = peak = dd = 0.0
    for d in sorted(days):
        eq += days[d]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"trades": trades, "net_pnl": round(sum(p["net_pnl"] for p in parts), 2),
            "charges": round(sum(p["charges"] for p in parts), 2),
            "win_rate_pct": round(100 * wins / trades, 1) if trades else 0.0,
            "profit_factor": round(won / lost, 2) if lost else None, "max_drawdown": round(dd, 2),
            "days": len(days), "green_days": sum(1 for v in days.values() if v > 0)}


def run(base: dict, bars_by_symbol: dict, grid: list[dict] | None = None, jobs: int | None = None,
        progress=None) -> dict:
    """bars_by_symbol: {symbol: [1-minute Bar, ...]}. Returns the report (see the module docstring)."""
    from bot import stock_backtest as SB
    grid = grid or combos()
    work, skipped, spans = [], [], {}
    for sym, bars in sorted(bars_by_symbol.items()):
        train, test = SB.split_walk_forward(bars)
        if not train or not test:
            skipped.append(f"{sym}: fewer than 6 trading days of candles")
            continue
        spans[sym] = (str(train[0].start.date()), str(train[-1].start.date()), str(test[0].start.date()), str(test[-1].start.date()))
        work.append((base, sym, train, test, grid))
    per: dict[str, dict] = {}
    jobs = max(1, jobs or (os.cpu_count() or 1))
    if jobs == 1:
        results = map(_one_stock, work)
    else:
        ex = ProcessPoolExecutor(max_workers=jobs)
        results = ex.map(_one_stock, work)
    for n, (sym, out) in enumerate(results, 1):
        per[sym] = dict(out)
        if progress:
            progress(n, len(work), sym)
    if jobs != 1:
        ex.shutdown()

    by_setting = []
    for i, c in enumerate(grid):
        tr = pool([per[s][i]["train"] for s in per])
        te = pool([per[s][i]["test"] for s in per])
        by_setting.append({"setting": key(c), **c, "train": tr, "test": te,
                           "stocks_up_test": sum(1 for s in per if per[s][i]["test"]["net_pnl"] > 0)})
    by_setting.sort(key=lambda r: -r["train"]["net_pnl"])

    chosen, picks = [], {}
    for s in per:                                     # per stock: best on the older days only, as the nightly check does
        ok = [(i, r) for i, r in per[s].items()
              if r["train"]["trades"] >= SB.min_trades(grid[i]["strategy"])[0] and r["train"]["net_pnl"] > 0]
        if not ok:
            continue
        i, r = max(ok, key=lambda ir: ir[1]["train"]["net_pnl"])
        picks[s] = {"setting": key(grid[i]), "train_net": r["train"]["net_pnl"], "test_net": r["test"]["net_pnl"],
                    "test_trades": r["test"]["trades"]}
        chosen.append(r["test"])
    span = list(spans.values())
    return {"stocks": len(per), "skipped": skipped, "combos": len(grid),
            "older_days": (min(x[0] for x in span), max(x[1] for x in span)) if span else None,
            "newer_days": (min(x[2] for x in span), max(x[3] for x in span)) if span else None,
            "by_setting": by_setting,
            "per_stock": {"stocks_with_a_choice": len(chosen), "test": pool(chosen) if chosen else None, "picks": picks}}


def report_text(rep: dict, top: int = 12) -> str:
    """The report as plain text for the terminal."""
    if not rep["stocks"]:
        return "No stock had at least 6 trading days of candles.\n" + "\n".join(rep["skipped"])
    lines = [f"{rep['stocks']} stocks · {rep['combos']} settings · older days {rep['older_days'][0]} to "
             f"{rep['older_days'][1]} (choose) · newer days {rep['newer_days'][0]} to {rep['newer_days'][1]} (confirm)",
             "Net is after fees. Settings ordered by the OLDER days; judge them on the NEWER-days columns.", ""]
    h = f"{'setting':<32}{'older net':>11}{'trades':>7} │ {'newer net':>10}{'trades':>7}{'win%':>6}{'PF':>6}{'maxDD':>10}{'up':>5}"
    lines += [h, "─" * len(h)]
    fmt = lambda v: "–" if v is None else f"{v:.2f}"
    few = 2 * rep["stocks"]                           # under two trades per stock on the older days: too little to rank
    traded = [r for r in rep["by_setting"] if r["train"]["trades"] >= few]
    idle = [f"{r['setting']} ({r['train']['trades']})" for r in rep["by_setting"] if r["train"]["trades"] < few]
    for r in traded[:top]:
        tr, te = r["train"], r["test"]
        lines.append(f"{r['setting']:<32}{tr['net_pnl']:>11.0f}{tr['trades']:>7} │ {te['net_pnl']:>10.0f}{te['trades']:>7}"
                     f"{te['win_rate_pct']:>6.1f}{fmt(te['profit_factor']):>6}{te['max_drawdown']:>10.0f}"
                     f"{r['stocks_up_test']:>3}/{rep['stocks']}")
    if idle:
        lines += ["", f"Too few trades on the older days to rank (fewer than {few}; trades in brackets): " + ", ".join(idle)]
    best_new = max(rep["by_setting"], key=lambda r: r["test"]["net_pnl"])
    lines += ["", f"Best on the newer days (hindsight, NOT a fair pick): {best_new['setting']}  "
                  f"net {best_new['test']['net_pnl']:.0f} over {best_new['test']['trades']} trades"]
    ps = rep["per_stock"]
    lines += ["", f"Per stock (best setting on its older days, judged on its newer days): "
                  f"{ps['stocks_with_a_choice']} of {rep['stocks']} stocks had a setting that made money on the older days"]
    if ps["test"]:
        t = ps["test"]
        lines.append(f"  newer days: net {t['net_pnl']:.0f} after {t['charges']:.0f} fees · {t['trades']} trades · "
                     f"win {t['win_rate_pct']}% · PF {fmt(t['profit_factor'])} · maxDD {t['max_drawdown']:.0f}")
    if rep["skipped"]:
        lines += ["", "Skipped: " + "; ".join(rep["skipped"])]
    return "\n".join(lines)
