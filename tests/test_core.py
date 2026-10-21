import time
from datetime import datetime, timedelta

import pytest

from bot.bars import BarBuilder
from bot.charges import order_charges
from bot.config import LIVE_CONFIRM_PHRASE, trading_mode, validate_config
from bot.instruments import load_master, resolve, round_to_tick
from bot.models import Tick
from bot.risk import OrderThrottle, RiskManager
from bot.strategies import build
from bot.timeutil import IST, floor_to
from conftest import FIXTURE_MASTER, make_bars


def ts(hhmmss):
    return datetime.fromisoformat(f"2026-10-14 {hhmmss}").replace(tzinfo=IST)


# ---------- bars ----------
def test_bar_builder_closes_on_next_minute_and_flush():
    out = []
    bb = BarBuilder(1, lambda tok, b: out.append(b))
    for t, px in [("09:20:01", 100), ("09:20:30", 102), ("09:20:59", 99), ("09:21:02", 101)]:
        bb.on_tick(Tick("1", px, ts(t)))
    assert len(out) == 1
    b = out[0]
    assert (b.open, b.high, b.low, b.close) == (100, 102, 99, 99)
    bb.flush(ts("09:22:03"))
    assert len(out) == 2 and out[1].close == 101


def test_floor_to_anchors_at_915():
    assert floor_to(ts("09:19:59"), 5).strftime("%H:%M") == "09:15"
    assert floor_to(ts("09:20:00"), 5).strftime("%H:%M") == "09:20"
    assert floor_to(ts("10:01:00"), 15).strftime("%H:%M") == "10:00"


# ---------- strategies ----------
def test_ema_cross_signals_up_then_down():
    s = build("ema_cross", {"fast": 3, "slow": 6})
    closes = [100] * 10 + [101, 102, 103, 104] + [103, 101, 99, 97, 95]
    bars, sigs = make_bars(closes), []
    for k in range(1, len(bars) + 1):
        r = s.on_bar(bars[:k], 0)
        if r is not None:
            sigs.append(r)
    assert sigs == [1, -1]


def test_orb_breakout_once_per_direction():
    s = build("orb", {"range_minutes": 15, "buffer_pct": 0.0})
    s.reset_day()
    closes = [100] * 15 + [100.2, 102, 103, 104]
    bars, sigs = make_bars(closes, spread=0.1), []
    for k in range(1, len(bars) + 1):
        r = s.on_bar(bars[:k], 0)
        if r is not None:
            sigs.append(r)
    assert sigs == [1]


def test_vwap_revert_long_when_far_below():
    s = build("vwap_revert", {"band_pct": 0.5})
    bars = make_bars([100] * 10 + [99.0], spread=0.0)
    assert s.on_bar(bars, 0) == 1
    assert s.on_bar(make_bars([100] * 10 + [99, 100.5], spread=0.0), 1) == 0


# ---------- risk ----------
def test_risk_gates(cfg):
    r = RiskManager(cfg)
    assert r.can_enter(ts("09:16:00"), 0, 1000)[0] is False          # before entry_start
    assert r.can_enter(ts("10:00:00"), 0, 1000) == (True, "ok")
    assert r.can_enter(ts("15:05:00"), 0, 1000)[0] is False          # after cutoff
    assert r.can_enter(ts("10:00:00"), 1, 1000)[0] is False          # max open positions
    assert r.can_enter(ts("10:00:00"), 0, 10**7)[0] is False         # order value
    assert r.check_pnl_limits(-10) is None
    assert "max_daily_loss" in r.check_pnl_limits(-600)
    assert r.can_enter(ts("10:00:00"), 0, 1000)[0] is False          # halted


def test_cooldown_after_consecutive_losses(cfg):
    r = RiskManager(cfg)
    for _ in range(3):
        r.on_trade_closed(-5, ts("10:00:00"))
    ok, why = r.can_enter(ts("10:10:00"), 0, 100)
    assert not ok and "cooldown" in why
    assert r.can_enter(ts("10:31:00"), 0, 100)[0]


def test_throttle_limits_rate():
    th = OrderThrottle(3)
    t0 = time.monotonic()
    for _ in range(6):
        th.acquire()
    assert time.monotonic() - t0 >= 0.9


# ---------- config / mode ----------
def test_config_validation_rejects_unsafe(cfg):
    bad = dict(cfg, risk=dict(cfg["risk"], max_orders_per_sec=12))
    with pytest.raises(ValueError, match="10 OPS"):
        validate_config(bad)
    bad = dict(cfg, exits=dict(cfg["exits"], stop_loss_pct=0))
    with pytest.raises(ValueError, match="stop"):
        validate_config(bad)


def test_live_mode_needs_two_keys(monkeypatch):
    monkeypatch.setenv("ANGELBOT_MODE", "live")
    monkeypatch.delenv("ANGELBOT_LIVE_CONFIRM", raising=False)
    with pytest.raises(RuntimeError):
        trading_mode()
    monkeypatch.setenv("ANGELBOT_LIVE_CONFIRM", LIVE_CONFIRM_PHRASE)
    assert trading_mode() == "live"
    monkeypatch.setenv("ANGELBOT_MODE", "paper")
    assert trading_mode() == "paper"


# ---------- instruments / charges ----------
def test_resolve_and_tick_rounding():
    insts = resolve([{"symbol": "SBIN-EQ", "exchange": "NSE", "qty": 2}], load_master(FIXTURE_MASTER))
    assert insts[0].token and insts[0].order_qty == 2 and insts[0].tick_size > 0
    assert round_to_tick(100.03, 0.05, "up") == 100.05
    assert round_to_tick(100.03, 0.05, "down") == 100.0
    assert round_to_tick(100.10, 0.05, "up") == 100.10


def test_charges_positive_and_sell_has_stt():
    c = {"brokerage_flat": 20, "brokerage_pct": 0.1}
    assert order_charges("SELL", 10, 800, c) > order_charges("BUY", 10, 800, c) > 0
