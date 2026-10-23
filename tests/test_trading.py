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


class _FakeFeed:
    connected = True
    restarts = 0

    def __init__(self):
        self.last_tick_at = None

    def restart(self):
        self.restarts += 1


def _live_engine(cfg, inst, tmp_path, monkeypatch):
    from bot import notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, buttons=None: sent.append(text))
    q = queue.Queue()
    eng = Engine(cfg, PaperBroker(0.0), _FakeFeed(), [inst], Journal(tmp_path / "j.db"), q)
    eng.now = lambda: t("09:25")
    return eng, sent


def test_morning_heartbeat_sent_once(cfg, inst, tmp_path, monkeypatch):
    eng, sent = _live_engine(cfg, inst, tmp_path, monkeypatch)
    eng.feed.last_tick_at = t("09:25")
    eng.ltp["SBIN-EQ"] = 800.0
    eng._periodic(); eng._periodic()
    beats = [m for m in sent if "bot is ready" in m]
    assert len(beats) == 1 and "SBIN-EQ" in beats[0] and "connected" in beats[0]


def test_feed_trouble_and_recovery_alerts(cfg, inst, tmp_path, monkeypatch):
    eng, sent = _live_engine(cfg, inst, tmp_path, monkeypatch)
    eng.feed.last_tick_at = t("09:25") - timedelta(seconds=120)      # stale → restart
    eng._periodic()
    assert any("Price feed problem" in m for m in sent) and eng.feed.restarts == 1
    eng.feed.last_tick_at = t("09:25")                               # ticks flowing again
    eng._periodic()
    assert any("Price feed is back" in m for m in sent)
    assert sum("Price feed problem" in m for m in sent) == 1


def test_cooldown_alert_after_consecutive_losses(cfg, inst, tmp_path, monkeypatch):
    eng, sent = _live_engine(cfg, inst, tmp_path, monkeypatch)
    n = int(cfg["risk"]["max_consecutive_losses"])
    for _ in range(n - 1):
        eng._on_trade_closed(-5.0, t("10:00"))
    assert not any("Taking a break" in m for m in sent)
    eng._on_trade_closed(-5.0, t("10:00"))
    assert sum("Taking a break" in m for m in sent) == 1 and "10:30" in sent[-1]


# ---------------------------------------------------------------- restart: the day's limits carry on
def _journal_with(tmp_path, trades, name="j.db"):
    j = Journal(tmp_path / name)
    for mode, hhmm, gross, fee in trades:
        j.trade(mode, "SBIN-EQ", "LONG", 5, t(hhmm) - timedelta(minutes=5), 800, t(hhmm), 800 + gross / 5, gross, fee,
                "stop_loss" if gross < 0 else "target", "ema_cross")
    return j


def _restored(cfg, inst, j, monkeypatch, now):
    from bot import notify
    monkeypatch.setattr(notify, "send", lambda text, buttons=None: None)
    eng = Engine(cfg, PaperBroker(0.0), _FakeFeed(), [inst], j, queue.Queue())
    eng.now = lambda: now
    eng.restore_day()
    return eng


def test_restart_rebuilds_trade_count_pnl_and_fees_from_today(cfg, inst, tmp_path, monkeypatch):
    j = _journal_with(tmp_path, [("paper", "09:40", 30.0, 3.0), ("paper", "10:10", -20.0, 3.0),
                                 ("live", "10:20", -500.0, 3.0)])               # another mode's trade is not counted
    j.trade("paper", "SBIN-EQ", "LONG", 5, t("09:30") - timedelta(days=1), 800, t("09:35") - timedelta(days=1), 790,
            -50.0, 3.0, "stop_loss", "ema_cross")                                # yesterday: not counted
    eng = _restored(cfg, inst, j, monkeypatch, t("11:00"))
    assert eng.risk.trades_today == 2 and eng.om.realized_net == pytest.approx(27.0 - 23.0)
    assert eng.om.charges_today == pytest.approx(6.0) and eng.restored["trades"] == 2
    assert eng.risk.consecutive_losses == 1


def test_restart_keeps_the_daily_loss_halt_and_the_trade_cap(cfg, inst, tmp_path, monkeypatch):
    loss = float(cfg["risk"]["max_daily_loss"])
    eng = _restored(cfg, inst, _journal_with(tmp_path, [("paper", "10:00", -loss, 5.0)]), monkeypatch, t("11:00"))
    assert eng.risk.check_pnl_limits(eng.om.realized_net)                       # still halted after the restart
    cap = int(cfg["risk"]["max_trades_per_day"])
    eng2 = _restored(cfg, inst, _journal_with(tmp_path, [("paper", f"10:{i:02d}", 1.0, 0.5) for i in range(cap)], "cap.db"), monkeypatch, t("11:00"))
    ok, why = eng2.risk.can_enter(t("11:00"), 0, 4000)
    assert not ok and why == "max_trades_per_day reached"


def test_restart_restores_a_running_cooldown_only(cfg, inst, tmp_path, monkeypatch):
    n = int(cfg["risk"]["max_consecutive_losses"])
    losses = [("paper", f"10:{i:02d}", -5.0, 1.0) for i in range(n)]             # the streak ends at 10:0(n-1)
    eng = _restored(cfg, inst, _journal_with(tmp_path, losses), monkeypatch, t("10:15"))
    mins = int(cfg["risk"]["cooldown_minutes"])
    assert eng.risk.cooldown_until == t(f"10:{n - 1:02d}") + timedelta(minutes=mins)
    assert eng.restored["cooldown_until"] == eng.risk.cooldown_until
    later = _restored(cfg, inst, _journal_with(tmp_path, losses, "later.db"),
                      monkeypatch, t("13:00"))
    assert later.risk.cooldown_until is None                                     # already over


def test_start_message_says_what_carried_over():
    from bot import messages as M
    text = M.started("paper", "ema_cross", ["SBIN-EQ"], None, {"trades": 3, "net": -12.5, "cooldown_until": t("10:30")})
    assert "Carried on from earlier today: 3 trade(s)" in text and "10:30" in text


def test_restart_restore_copes_with_old_rows_without_a_timezone(cfg, inst, tmp_path, monkeypatch):
    n = int(cfg["risk"]["max_consecutive_losses"])                        # enough losses to start a cooldown
    j = _journal_with(tmp_path, [("paper", f"10:{i:02d}", -5.0, 1.0) for i in range(n)])
    with j.lock:
        j.conn.execute("UPDATE trades SET exit_ts = substr(exit_ts, 1, 19)")     # '2026-10-14T10:00:00', no +05:30
        j.conn.commit()
    eng = _restored(cfg, inst, j, monkeypatch, t("10:15"))              # still inside the cooldown
    assert eng.risk.trades_today == n and eng.om.realized_net == pytest.approx(-6.0 * n)
    assert eng.risk.cooldown_until is not None and eng.risk.cooldown_until.tzinfo is not None


def test_feed_watchdog_backs_off_and_allows_the_open(cfg, inst, tmp_path, monkeypatch):
    import bot.engine as E
    eng, sent = _live_engine(cfg, inst, tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(E.time, "monotonic", lambda: clock[0])
    eng.feed.connected = True
    eng.feed.last_tick_at = t("09:07")                       # pre-open: the last tick is before 09:15
    eng.now = lambda: t("09:15", 10)
    eng._periodic()
    assert eng.feed.restarts == 0                             # not stale yet: counted from the open
    eng.now = lambda: t("10:00")                              # now really stale; restarts keep failing
    gaps, last_restart = [], None
    for step in range(0, 1200, 5):
        clock[0] = 1000.0 + step
        before = eng.feed.restarts
        eng._periodic()
        if eng.feed.restarts > before:
            if last_restart is not None:
                gaps.append(step - last_restart)
            last_restart = step
    assert gaps[:4] == [35, 65, 125, 245] and all(g <= 305 for g in gaps)   # 30 s, 1, 2, 4 min, then capped at 5
    eng.feed.last_tick_at = t("10:00")                        # ticks flow again: back to normal and reset
    eng._periodic()
    assert eng._feed_wait == 0.0 and any("Price feed is back" in m for m in sent)
