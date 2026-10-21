import queue
from datetime import datetime, timedelta

from bot.backtest import run_backtest
from bot.engine import Engine
from bot.feed import ReplayFeed
from bot.journal import Journal, query_readonly
from bot.orders import OrderManager
from bot.paper import PaperBroker
from bot.timeutil import IST
from conftest import make_bars

import pytest


def t(hhmm, s=0):
    return datetime.fromisoformat(f"2026-10-14 {hhmm}").replace(tzinfo=IST) + timedelta(seconds=s)


def test_order_manager_entry_fill_and_stop_exit(cfg, inst, tmp_path):
    j = Journal(tmp_path / "j.db")
    closed = []
    pb = PaperBroker(0.0)
    om = OrderManager(pb, j, cfg, [inst], "test", lambda pnl, now: closed.append(pnl))
    pb.on_tick(inst.token, 800.0)
    oid = om.enter("SBIN-EQ", 1, 800.0, t("10:00"), "test")
    om.poll(t("10:00", 2), {"SBIN-EQ": 800.0}, force=True)
    pos = om.positions["SBIN-EQ"]
    assert pos.qty == 1 and abs(pos.avg_price - 800.0) < 0.01
    assert pos.stop == pytest.approx(800 * (1 - 0.003))
    pb.on_tick(inst.token, 797.0)
    om.exit("SBIN-EQ", 797.0, t("10:05"), "stop_loss")
    om.poll(t("10:05", 2), {"SBIN-EQ": 797.0}, force=True)
    assert pos.qty == 0 and len(closed) == 1 and closed[0] < -3   # -3 gross minus charges
    rows = query_readonly(tmp_path / "j.db", "SELECT * FROM trades")
    assert rows[0]["exit_reason"] == "stop_loss" and rows[0]["charges"] > 0


def test_exit_chase_reprices_until_filled(cfg, inst, tmp_path):
    j = Journal(tmp_path / "j.db")
    pb = PaperBroker(0.0)
    om = OrderManager(pb, j, cfg, [inst], "test", lambda *a: None)
    pb.on_tick(inst.token, 800.0)
    om.enter("SBIN-EQ", 1, 800.0, t("10:00"), "x")
    om.poll(t("10:00", 1), {}, force=True)
    pb.on_tick(inst.token, 795.0)                          # market drops fast …
    om.exit("SBIN-EQ", 800.0, t("10:01"), "signal")       # … exit priced off a stale 800 → SELL ~799.2 rests
    om.poll(t("10:01", 2), {"SBIN-EQ": 795.0}, force=True)
    assert om.positions["SBIN-EQ"].qty == 1                # not yet chased (timeout 4s)
    om.poll(t("10:01", 6), {"SBIN-EQ": 795.0}, force=True) # chase → reprice below 795
    om.poll(t("10:01", 7), {"SBIN-EQ": 795.0}, force=True)
    assert om.positions["SBIN-EQ"].qty == 0


def test_entry_timeout_cancels(cfg, inst, tmp_path):
    om = OrderManager(PaperBroker(0.0), Journal(tmp_path / "j.db"), cfg, [inst], "t", lambda *a: None)
    om.broker.on_tick(inst.token, 800.0)
    # place a BUY far below market so it rests
    om._place(inst, "BUY", 1, 780.0, "ENTRY", t("10:00"), "rest")
    om.poll(t("10:00", 9), {}, force=True)   # > entry_timeout_sec → cancel requested
    om.poll(t("10:00", 10), {}, force=True)
    assert not om.pending and om.positions["SBIN-EQ"].qty == 0


def _day_candles(closes, day="2026-10-14"):
    bars = make_bars(closes, start=f"{day} 09:15", spread=0.2)
    return [[b.start.isoformat(), b.open, b.high, b.low, b.close, 1000] for b in bars]


def test_engine_replay_full_day_ends_flat(cfg, inst, tmp_path):
    import math
    closes = [800 + 6 * math.sin(k / 18) + k * 0.01 for k in range(375)]
    q = queue.Queue()
    j = Journal(tmp_path / "j.db")
    cfg["strategy"]["params"]["ema_cross"] = {"fast": 5, "slow": 13}
    eng = Engine(cfg, PaperBroker(0.0), ReplayFeed({inst.token: _day_candles(closes)}, q), [inst], j, q, replay=True)
    eng.run()
    assert eng.om.open_positions() == 0 and not eng.om.pending
    trades = query_readonly(tmp_path / "j.db", "SELECT * FROM trades")
    assert len(trades) == eng.risk.trades_today > 0
    assert all(tr["entry_ts"] >= "2026-10-14T09:20" for tr in trades)        # entry window
    assert all(tr["entry_ts"] < "2026-10-14T15:01" for tr in trades)
    assert round(sum(tr["net_pnl"] for tr in trades), 2) == round(eng.om.realized_net, 2)


def test_engine_daily_loss_halts_and_flattens(cfg, inst, tmp_path):
    # ORB long breakout, then a steady slide; ORB never emits an exit signal, so only
    # the DAILY loss limit can get us out (per-trade stop is disabled below).
    closes = [800] * 16 + [801, 802] + [802 - 2 * k for k in range(1, 60)] + [690] * 100
    cfg["strategy"]["name"] = "orb"
    cfg["exits"]["stop_loss_pct"] = 50          # disable per-trade stop so the DAILY limit must act
    cfg["exits"]["target_pct"] = 0
    cfg["risk"]["max_daily_loss"] = 2
    q = queue.Queue()
    eng = Engine(cfg, PaperBroker(0.0), ReplayFeed({inst.token: _day_candles(closes)}, q), [inst],
                 Journal(tmp_path / "j.db"), q, replay=True)
    eng.run()
    assert eng.risk.halted_reason and "max_daily_loss" in eng.risk.halted_reason
    assert eng.om.open_positions() == 0
    assert eng.om.realized_net > -10            # flattened near the limit, not at the bottom (690)


def test_kill_switch_flattens(cfg, inst, tmp_path):
    from bot import control
    closes = [800] * 30 + [800 + 0.5 * k for k in range(1, 60)] + [830] * 100
    cfg["strategy"]["params"]["ema_cross"] = {"fast": 3, "slow": 8}
    cfg["exits"]["target_pct"] = 0
    q = queue.Queue()
    eng = Engine(cfg, PaperBroker(0.0), ReplayFeed({inst.token: _day_candles(closes)}, q), [inst],
                 Journal(tmp_path / "j.db"), q, replay=True)
    orig = eng._on_tick
    def hook(tick, vol):
        orig(tick, vol)
        if eng.om.open_positions() and not eng.control["kill"]:
            control.set_control(kill=True, note="test")
    eng._on_tick = hook
    eng.run()
    assert eng.flatten_reason == "KILL SWITCH" and eng.om.open_positions() == 0
    c = control.read_control()
    assert c["kill"] is False and c["paused"] is True      # auto-reset → paused
    control.set_control(paused=False, kill=False)


def test_backtest_long_hits_target(cfg):
    cfg["strategy"]["params"]["ema_cross"] = {"fast": 3, "slow": 8}
    closes = [800] * 20 + [800 + 0.8 * k for k in range(1, 40)] + [830] * 30
    res = run_backtest(cfg, make_bars(closes, start="2026-10-14 09:15", spread=0.1))
    assert res["trades"] >= 1 and res["exit_reasons"].get("target", 0) >= 1 and res["gross_pnl"] > 0


def test_backtest_matches_replay_roughly(cfg, inst, tmp_path):
    import math, random
    rnd = random.Random(3)
    px, closes = 800.0, []
    for _ in range(375):
        px *= 1 + rnd.gauss(0, 0.0012); closes.append(px)
    cfg["strategy"]["params"]["ema_cross"] = {"fast": 9, "slow": 21}
    bt = run_backtest(cfg, make_bars(closes, start="2026-10-14 09:15", spread=0.2))
    q = queue.Queue()
    eng = Engine(cfg, PaperBroker(0.02), ReplayFeed({inst.token: _day_candles(closes)}, q), [inst],
                 Journal(tmp_path / "j.db"), q, replay=True)
    eng.run()
    assert abs(bt["trades"] - eng.risk.trades_today) <= max(2, bt["trades"] // 3)
