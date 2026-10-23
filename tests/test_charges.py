"""Angel One NSE equity intraday charges, checked against hand-worked figures (rates from config.yaml)."""
from datetime import datetime

import pytest

from bot.charges import breakdown, order_charges
from bot.config import load_config
from bot.models import OrderRecord
from bot.timeutil import IST

C = load_config()["charges"]


def test_config_has_the_published_rates():
    assert (C["brokerage_flat"], C["brokerage_pct"], C["brokerage_min"]) == (20, 0.1, 5)
    assert C["exchange_txn_pct"] == pytest.approx(0.0030699) and C["ipft_pct"] == pytest.approx(0.0000001)
    assert (C["sebi_per_crore"], C["stt_sell_pct"], C["stamp_buy_pct"], C["gst_pct"]) == (10, 0.025, 0.003, 18)


def test_buy_5_tcs_at_2575_worked_by_hand():
    b = breakdown("BUY", 5 * 2575, C)                       # value ₹12,875
    assert b["brokerage"] == 12.875                         # 0.1% = ₹12.875, under the ₹20 cap, over the ₹5 minimum
    assert b["exchange_txn"] == pytest.approx(0.3952, abs=1e-4)   # 12,875 x 0.0030699%
    assert b["sebi"] == pytest.approx(0.0129, abs=1e-4)     # 12,875 x ₹10 / 1 crore
    assert b["stamp"] == pytest.approx(0.3863, abs=1e-4)    # 12,875 x 0.003%
    assert b["stt"] == 0                                    # STT is sell side only
    assert b["gst"] == pytest.approx(0.18 * (12.875 + 0.3952496 + 0.012875 + 0.000012875), abs=1e-4)
    assert b["total"] == 16.06


def test_sell_side_has_stt_rounded_to_the_rupee():
    b = breakdown("SELL", 5 * 2575, C)
    assert b["stt"] == 3.0 and b["stamp"] == 0              # 12,875 x 0.025% = ₹3.21875 -> ₹3
    assert b["total"] == 18.67
    assert order_charges("BUY", 5, 2575, C) + order_charges("SELL", 5, 2575, C) == pytest.approx(34.73)
    assert breakdown("SELL", 50_000, C)["stt"] == 13.0      # ₹12.50 rounds up
    assert breakdown("SELL", 49_960, C)["stt"] == 12.0      # ₹12.49 rounds down
    assert breakdown("SELL", 12_875, {**C, "stt_round_rupee": False})["stt"] == pytest.approx(3.21875, abs=1e-4)


def test_brokerage_cap_minimum_and_exchange_maximum():
    assert breakdown("BUY", 40_000, C)["brokerage"] == 20.0     # 0.1% = ₹40, capped at ₹20
    assert breakdown("BUY", 800, C)["brokerage"] == 5.0         # 0.1% = ₹0.80, raised to the ₹5 minimum
    assert breakdown("BUY", 100, C)["brokerage"] == 2.5         # ₹5 would be 5% of ₹100: held to the 2.5% maximum
    assert breakdown("BUY", 0, C)["total"] == 0.0


def test_partial_fills_of_one_order_pay_one_brokerage(cfg, inst, tmp_path, monkeypatch):
    from test_trading import _live_engine
    cfg["charges"] = C
    eng, _ = _live_engine(cfg, inst, tmp_path, monkeypatch)
    rec = OrderRecord("O1", "SBIN-EQ", "BUY", 50, 1000.0, "ENTRY", datetime(2026, 10, 23, 10, tzinfo=IST))
    eng.om._apply_fill(rec, 20, 1000.0, rec.placed_at)       # 20 shares now, 30 a moment later
    eng.om._apply_fill(rec, 30, 1000.0, rec.placed_at)
    assert eng.om.charges_today == pytest.approx(order_charges("BUY", 50, 1000.0, C), abs=0.01)
    two_orders = order_charges("BUY", 20, 1000.0, C) + order_charges("BUY", 30, 1000.0, C)
    assert two_orders - eng.om.charges_today > 15            # separate orders would pay brokerage twice
