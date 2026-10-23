import queue
from datetime import datetime

import pytest

from bot import sizing
from bot import settings as ST
from bot.backtest import run_backtest
from bot.engine import Engine
from bot.journal import Journal
from bot.models import Instrument
from bot.paper import PaperBroker
from bot.screener import gates, metrics, params_from
from bot.telegram_ctl import route
from bot.timeutil import IST
from conftest import make_bars
from test_screener import CHARGES, raw
from test_trading import _FakeFeed


def test_qty_never_costs_more_than_the_amount():
    assert sizing.qty_for(10000, 812.35) == 12 and 12 * 812.35 <= 10000 < 13 * 812.35
    assert sizing.qty_for(5000, 812.35) == 6
    assert sizing.qty_for(500, 812.35) == 0                      # not even one share fits: no trade
    assert sizing.qty_for(10000, 120.0, lot_size=25) == 75       # whole lots only: 3 x 25
    assert sizing.qty_for(0, 800) == 0 and sizing.qty_for(1000, 0) == 0


def test_validate_amount():
    sizing.validate({"risk": {"max_order_value": 50000}, "sizing": {"amount": 7500}})
    sizing.validate({"risk": {"max_order_value": 50000}})
    for bad in (-1, 50, 60000):
        with pytest.raises(ValueError):
            sizing.validate({"risk": {"max_order_value": 50000}, "sizing": {"amount": bad}})


def _engine(cfg, inst, tmp_path, monkeypatch, amount):
    from bot import notify
    monkeypatch.setattr(notify, "send", lambda text, buttons=None: None)
    cfg["sizing"] = {"amount": amount}
    eng = Engine(cfg, PaperBroker(0.0), _FakeFeed(), [inst], Journal(tmp_path / "j.db"), queue.Queue())
    eng.now = lambda: datetime(2026, 10, 14, 10, 0, tzinfo=IST)
    placed = []
    monkeypatch.setattr(eng.om, "enter", lambda sym, d, ltp, now, reason, qty=None: placed.append(qty) or "OID1")
    return eng, placed


def test_entry_shares_come_from_the_amount_at_the_limit_price(cfg, inst, tmp_path, monkeypatch):
    eng, placed = _engine(cfg, inst, tmp_path, monkeypatch, 10000)
    eng.ltp["SBIN-EQ"] = 800.0
    ok, why = eng._try_enter(inst, 1, 800.0)
    limit = eng.om.entry_price("SBIN-EQ", 1, 800.0)                     # LTP + entry buffer
    assert ok and placed == [int(10000 // limit)] and placed[0] * limit <= 10000


def test_entry_skipped_when_one_share_costs_more_than_the_amount(cfg, inst, tmp_path, monkeypatch):
    eng, placed = _engine(cfg, inst, tmp_path, monkeypatch, 500)
    eng.ltp["SBIN-EQ"] = 800.0
    ok, why = eng._try_enter(inst, 1, 800.0)
    assert not ok and "buys no share" in why and placed == []


def test_no_amount_keeps_the_configured_shares(cfg, inst, tmp_path, monkeypatch):
    eng, placed = _engine(cfg, inst, tmp_path, monkeypatch, 0)
    eng.ltp["SBIN-EQ"] = 800.0
    assert eng._try_enter(inst, 1, 800.0)[0] and placed == [inst.order_qty]


def test_backtest_sizes_each_trade_by_the_amount(cfg, tmp_path):
    import csv
    closes = [800 + i * 0.5 for i in range(60)] + [830 - i * 0.6 for i in range(60)] + [794 + i * 0.7 for i in range(120)]
    bars = make_bars(closes, start="2026-10-14 09:15")
    cfg["sizing"] = {"amount": 9000}
    res = run_backtest(cfg, bars, qty=1, out_csv=tmp_path / "t.csv")
    assert res["trades"] >= 1 and res["amount_per_trade"] == 9000
    rows = list(csv.DictReader(open(tmp_path / "t.csv")))
    assert rows and all(int(r["qty"]) * float(r["entry"]) <= 9000.01 for r in rows)      # entry is rounded to paise
    assert all(int(r["qty"]) > 1 for r in rows)                       # sized by the amount, not the qty=1 argument


def test_screener_sizes_by_the_amount_and_leaves_out_too_expensive_stocks():
    p = params_from({"sizing": {"amount": 5000}, "selection": {"mode": "auto", "qty": 5}, "risk": {"max_order_value": 50000}})
    assert p["amount"] == 5000 and p["qty"] == 0 and p["order_value"] == 5000
    m = metrics(raw("OK"), p, CHARGES)                                 # 1000 a share: 5 fit
    assert m["suggested_qty"] == 5 and gates(m, p) == []
    pricey = metrics(raw("PRICEY", ltp=12500.0, bid=12499.0, ask=12501.0), p, CHARGES)
    assert gates(pricey, p) == ["one share costs ₹12,500, more than your ₹5,000 per trade"]


@pytest.fixture
def clean_settings(monkeypatch):
    ST.path().unlink(missing_ok=True)
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    yield
    ST.path().unlink(missing_ok=True)


def test_amount_from_telegram_buttons_and_typed(clean_settings):
    text, rows = route("/amount")
    assert "Amount per trade" in text and "/amount 7500" in text
    text, _ = route("/amount 7,500")
    assert "Saved" in text and "₹7,500" in text and "not used: sized by amount" in text
    assert ST.load()["sizing"]["amount"] == 7500
    assert "above risk.max_order_value" in route("/amount 900000")[0] and ST.load()["sizing"]["amount"] == 7500
    assert "Not a number" in route("/amount lots")[0]
    text, _ = route("/amount off")
    base = float((ST.C.load_config().get("sizing") or {}).get("amount") or 0)
    assert "off (use the share counts)" in text and ST.load()["sizing"]["amount"] == 0
    assert ST.overrides() == ({} if base == 0 else {"sizing.amount": 0})


# ---------------------------------------------------------------- edge cases
def test_bad_amounts_are_refused_and_never_crash_sizing():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            sizing.validate({"risk": {"max_order_value": 50000}, "sizing": {"amount": bad}})
        assert sizing.qty_for(bad, 800) == 0 and sizing.qty_for(5000, bad) == 0
    assert sizing.qty_for(800, 800) == 1                                   # exactly one share fits


def test_typed_amount_edge_cases(clean_settings):
    for word in ("nan", "inf", "-500", "50", "1e9"):
        text, _ = route(f"/amount {word}")
        assert "Saved" not in text, word
    assert ST.overrides() == {}
    assert "Saved" in route("/amount ₹2,500")[0] and ST.load()["sizing"]["amount"] == 2500
    with pytest.raises(ST.SettingsError):
        ST.apply_value("selqty", 7)                                        # only the amount takes typed values


def test_short_entry_is_sized_on_the_higher_of_ltp_and_limit(cfg, inst, tmp_path, monkeypatch):
    cfg["strategy"]["allow_short"] = True
    eng, placed = _engine(cfg, inst, tmp_path, monkeypatch, 8000)
    eng.ltp["SBIN-EQ"] = 800.0
    assert eng._try_enter(inst, -1, 800.0)[0]
    assert placed == [10] and placed[0] * 800.0 <= 8000                    # the sell limit is below LTP: size on LTP


def test_reversal_with_no_price_yet_does_not_crash(cfg, inst, tmp_path, monkeypatch):
    eng, placed = _engine(cfg, inst, tmp_path, monkeypatch, 5000)
    ok, why = eng._try_enter(inst, 1, 0)                                   # pending reversal before any tick
    assert not ok and "buys no share" in why and placed == []


def test_backtest_amount_too_small_means_no_trades(cfg):
    closes = [800 + i * 0.5 for i in range(60)] + [830 - i * 0.6 for i in range(60)] + [794 + i * 0.7 for i in range(120)]
    cfg["sizing"] = {"amount": 500}                                        # less than one share
    assert run_backtest(cfg, make_bars(closes, start="2026-10-14 09:15"), qty=1)["trades"] == 0


def test_damaged_or_hand_edited_settings_are_ignored_and_reported(clean_settings):
    ST.path().parent.mkdir(parents=True, exist_ok=True)
    ST.path().write_text("values: [oops\n")
    assert ST.load()["risk"]["max_daily_loss"] > 0                          # the bot still starts on config.yaml
    assert "damaged" in ST.problems()[0] and "damaged" in route("/settings")[0]
    ST.path().write_text("values:\n  selection.qty: abc\n  sizing.amount: .nan\n  risk.max_daily_loss: 1000\n  x.y: 1\n")
    assert ST.overrides() == {"risk.max_daily_loss": 1000}
    assert len(ST.problems()) == 3 and ST.load()["selection"]["qty"] != "abc"
    assert "Saved" in route("/amount 3000")[0]                              # a save rewrites a clean file
    assert ST.problems() == [] and ST.overrides() == {"risk.max_daily_loss": 1000, "sizing.amount": 3000}
