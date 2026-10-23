from datetime import timedelta

import pytest

from bot import messages as M
from bot.models import Bar
from bot.strategies import build
from conftest import make_bars


def _plus(bars, close):
    last = bars[-1]
    return bars + [Bar(last.start + timedelta(minutes=1), last.close, max(last.close, close), min(last.close, close), close, 1000)]


@pytest.mark.parametrize("closes", [
    [800 - i * 0.2 for i in range(40)],                       # falling: fast below slow, next is a buy
    [800 + i * 0.2 for i in range(40)],                       # rising: fast above slow, next is a sell
])
def test_ema_cross_price_is_exactly_where_the_next_bar_crosses(closes):
    s = build("ema_cross")
    bars = make_bars(closes)
    st = s.state(bars, ltp=bars[-1].close)
    assert st["bars"] == 40 and st["needed"] == 22 and st["fast_above"] == (closes[-1] > closes[0])
    want = 1 if st["next_signal"] == "buy" else -1
    nudge = 0.01 * want                                        # a hair past the cross price
    assert s.on_bar(_plus(bars, st["cross_price"] + nudge), 0) == want
    assert s.on_bar(_plus(bars, st["cross_price"] - nudge), 0) is None
    assert st["cross_move_pct"] == pytest.approx((st["cross_price"] - bars[-1].close) / bars[-1].close * 100, abs=1e-3)


def test_ema_cross_warming_up():
    st = build("ema_cross").state(make_bars([800.0] * 10))
    assert st == {"bars": 10, "needed": 22}
    assert M.strategy_state_lines("TCS-EQ", st) == ["🔍 <b>TCS-EQ</b>: warming up, 10 of 22 one-minute bars"]


def test_orb_and_vwap_states():
    bars = make_bars([800 + (i % 5) for i in range(30)])
    orb = build("orb").state(bars)
    assert orb["range_done"] and orb["buy_above"] > orb["range_high"] > orb["range_low"] > orb["sell_below"]
    v = build("vwap_revert").state(bars, ltp=790.0)
    assert v["buy_below"] < v["vwap"] < v["sell_above"] and v["dev_pct"] < 0
    early = build("orb").state(bars[:5])
    assert early["range_done"] is False and "building the opening range" in M.strategy_state_lines("X", early)[0]
    assert "buy above" in M.strategy_state_lines("X", orb)[1] and "VWAP" in M.strategy_state_lines("X", v)[0]


def test_status_card_shows_the_state_in_plain_words():
    s = {"written_at": "2026-10-23T10:11:48+05:30", "bot_alive": True, "mode": "paper", "strategy": "ema_cross(fast=9, slow=21)",
         "day_pnl": 0, "trades_today": 0, "positions": [],
         "strategy_state": {"TCS-EQ": {"bars": 49, "needed": 22, "fast_ema": 2574.1, "slow_ema": 2575.3, "fast_above": False,
                                       "gap_pct": -0.047, "next_signal": "buy", "cross_price": 2576.8, "cross_move_pct": 0.093}}}
    text = M.status_card(s, {"paused": False, "kill": False})
    assert "TCS-EQ</b> · 49 bars · fast avg ₹2,574.10 below slow avg ₹2,575.30 (−0.05%)" in text
    assert "Buy signal if a minute closes above ₹2,576.80 (+0.09% from now)" in text
    assert "state unavailable" in M.strategy_state_lines("X", {"error": "boom"})[0]


def test_engine_writes_the_state_and_survives_a_broken_one(cfg, inst, tmp_path, monkeypatch):
    from bot import control
    from test_trading import _live_engine, t
    eng, _ = _live_engine(cfg, inst, tmp_path, monkeypatch)
    eng.prefill({"SBIN-EQ": make_bars([800 - i * 0.2 for i in range(30)])})
    eng.ltp["SBIN-EQ"] = 794.0
    eng._write_status(0.0)
    st = control.read_status()["strategy_state"]["SBIN-EQ"]
    assert st["bars"] == 30 and st["next_signal"] == "buy" and "cross_price" in st
    monkeypatch.setattr(eng.strategies["SBIN-EQ"], "state", lambda *a: 1 / 0)
    eng._write_status(0.0)                                     # display only: never stops the bot
    assert "error" in control.read_status()["strategy_state"]["SBIN-EQ"]
