import random
from datetime import datetime, timedelta

from bot import config as C, stock_backtest as SB, wf_grid as G
from bot.models import Bar
from bot.timeutil import IST


def walk(seed, days=9, start=1000.0):
    rnd, bars, px = random.Random(seed), [], start
    d = datetime(2026, 10, 1, 9, 15, tzinfo=IST)
    while len({b.start.date() for b in bars}) < days:
        if d.weekday() < 5:
            t = d
            for _ in range(375):
                o = px
                px = max(1.0, px * (1 + rnd.gauss(0, 0.0012)))
                bars.append(Bar(t, o, max(o, px) * 1.0003, min(o, px) * 0.9997, px, 1000))
                t += timedelta(minutes=1)
        d += timedelta(days=1)
    return bars


def test_pool_adds_runs_and_measures_drawdown_on_summed_days():
    a = {"trades": 2, "wins": 1, "net_pnl": -10.0, "charges": 4.0, "net_won": 5.0, "net_lost": 15.0,
         "daily": {"2026-10-01": 5.0, "2026-10-02": -15.0}}
    b = {"trades": 1, "wins": 1, "net_pnl": 8.0, "charges": 2.0, "net_won": 8.0, "net_lost": 0.0,
         "daily": {"2026-10-02": 8.0}}
    p = G.pool([a, b])
    assert (p["trades"], p["net_pnl"], p["win_rate_pct"], p["profit_factor"]) == (3, -2.0, 66.7, 0.87)
    assert p["max_drawdown"] == -7.0 and p["days"] == 2 and p["green_days"] == 1      # +5, then 5 - 7 = -2


def test_cfg_for_sets_bars_size_exits_and_scales_the_daily_loss_limit():
    base = C.load_config()
    base["sizing"]["amount"], base["risk"]["max_daily_loss"] = 10000, 500
    cfg = G.cfg_for(base, {"strategy": "orb", "tf": 15, "amount": 50000, "stop": 1.0, "target": 2.0})
    assert cfg["strategy"]["timeframe_min"] == 15 and cfg["sizing"]["amount"] == 50000
    assert (cfg["exits"]["stop_loss_pct"], cfg["exits"]["target_pct"]) == (1.0, 2.0)
    assert cfg["risk"]["max_daily_loss"] == 2500 and base["risk"]["max_daily_loss"] == 500
    assert base["strategy"].get("timeframe_min") == 1                                  # the base is not changed


def test_run_reports_by_setting_and_per_stock_on_the_newer_days_only():
    grid = G.combos(strategies=("ema_cross", "orb"), timeframes=(5,), amounts=(10000,), exits=((0.5, 1.0),))
    rep = G.run(C.load_config(), {"AAA-EQ": walk(1), "BBB-EQ": walk(2), "CCC-EQ": walk(3, days=4)}, grid, jobs=1)
    assert rep["stocks"] == 2 and rep["combos"] == 2 and "CCC-EQ" in rep["skipped"][0]
    assert rep["older_days"][1] < rep["newer_days"][0]                                 # no day in both parts
    trains = [r["train"]["net_pnl"] for r in rep["by_setting"]]
    assert trains == sorted(trains, reverse=True)                                      # ordered by the older days
    for r in rep["by_setting"]:
        assert {"trades", "net_pnl", "win_rate_pct", "profit_factor", "max_drawdown"} <= set(r["test"])
    text = G.report_text(rep)
    assert "older days" in text and "newer days" in text and "Per stock" in text


def test_opening_range_needs_fewer_trades_in_the_walk_forward_check():
    assert SB.min_trades("orb") == (6, 3) and SB.min_trades("ema_cross") == (12, 5)
    s = lambda n, net: {"trades": n, "net_pnl": net}
    c = SB.choose({"orb": {"train": s(7, 40.0), "test": s(3, 10.0)}})
    assert c["strategy"] == "orb"                                                      # 7 trades were not enough before
    assert SB.choose({"ema_cross": {"train": s(7, 40.0), "test": s(3, 10.0)}})["strategy"] is None
