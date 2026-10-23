import copy
import json
from datetime import datetime, timedelta

import pytest

from bot import control, screen_data
from bot import settings as ST
from bot import stock_backtest as SB
from bot.models import Instrument
from bot.screen_report import render_html, summary_html
from bot.screener import gates, metrics, params_from, rank
from bot.telegram_ctl import callback
from bot.timeutil import IST
from conftest import FIXTURE_MASTER, make_bars
from test_screener import CHARGES, NOW, raw

EVENING = datetime(2026, 10, 21, 15, 45, tzinfo=IST)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for p in (SB.path(), ST.path(), control.STATUS):
        p.unlink(missing_ok=True)
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    yield
    for p in (SB.path(), ST.path(), control.STATUS):
        p.unlink(missing_ok=True)


def inst(sym, token="1"):
    return Instrument(sym, token, "NSE", 1, 0.05, 1, "INTRADAY")


def candles(days=6):
    """[[iso, o, h, l, c, v], ...]: a few trending-then-reversing sessions, enough for ema_cross to trade."""
    rows = []
    for d in range(days):
        closes = [800 + i * 0.5 for i in range(60)] + [830 - i * 0.6 for i in range(60)] + [794 + i * 0.7 for i in range(120)]
        for b in make_bars(closes, start=f"2026-10-{12 + d:02d} 09:15"):
            rows.append([b.start.isoformat(), b.open, b.high, b.low, b.close, b.volume])
    return rows


def test_fingerprint_follows_what_the_results_depend_on(cfg):
    base = SB.fingerprint(cfg)
    other = copy.deepcopy(cfg)
    other["session"]["holidays"] = ["2026-12-31"]
    assert SB.fingerprint(other) == base                                   # a new holiday does not matter
    for change in (lambda c: c["strategy"].update(name="orb"), lambda c: c["exits"].update(stop_loss_pct=0.5),
                   lambda c: c.update(sizing={"amount": 5000}), lambda c: c["risk"].update(max_daily_loss=900)):
        c = copy.deepcopy(cfg)
        change(c)
        assert SB.fingerprint(c) != base


def test_run_all_backtests_each_stock_and_keeps_going_after_a_failure(cfg):
    def fetch(broker, i, start, end):
        if i.symbol == "BAD-EQ":
            raise RuntimeError("rate limited")
        return [] if i.symbol == "EMPTY-EQ" else candles()
    seen = []
    snap = SB.run_all(cfg, None, [inst("A-EQ"), inst("BAD-EQ"), inst("EMPTY-EQ")], EVENING, fetch_fn=fetch,
                      progress=lambda n, tot, sym: seen.append(sym))
    assert seen == ["A-EQ", "BAD-EQ", "EMPTY-EQ"] and list(snap["results"]) == ["A-EQ"]
    r = snap["results"]["A-EQ"]
    assert r["trades"] > 0 and set(r) == {"trades", "net_pnl", "charges", "win_rate_pct", "profit_factor", "max_drawdown", "days"}
    assert any("BAD-EQ: rate limited" in e for e in snap["errors"]) and any("EMPTY-EQ: no candles" in e for e in snap["errors"])
    assert snap["fingerprint"] == SB.fingerprint(cfg) and snap["days"] == 30


def test_load_fresh_refuses_missing_old_damaged_or_changed_results(cfg):
    assert SB.load_fresh(cfg, NOW) == (None, "no stock backtests yet")
    SB.save({"run_at": EVENING.isoformat(), "fingerprint": SB.fingerprint(cfg), "results": {"A-EQ": {"trades": 30}}})
    assert SB.load_fresh(cfg, NOW)[0] == {"A-EQ": {"trades": 30}}                       # Tuesday evening, used Thursday
    assert "days old" in SB.load_fresh(cfg, EVENING + timedelta(days=5))[1]
    changed = copy.deepcopy(cfg)
    changed["strategy"]["name"] = "orb"
    assert "changed since" in SB.load_fresh(changed, NOW)[1]
    SB.path().write_text(json.dumps({"run_at": "yesterday"}))
    assert "damaged" in SB.load_fresh(cfg, NOW)[1]


def _p():
    return params_from({"screener": {"backtest_gate": True}})


def test_gate_reasons():
    p = _p()
    def why(bt, **pk):
        m = metrics(raw("X", bt=bt), {**p, **pk}, CHARGES)
        return gates(m, {**p, **pk})
    assert why({"trades": 34, "net_pnl": 120.5, "days": 30}) == []
    assert why(None) == ["no recent backtest for this stock"]
    assert why({"trades": 5, "net_pnl": 50.0, "days": 30}) == ["backtest made only 5 trades in 30 days (need 20)"]
    assert why({"trades": 40, "net_pnl": -88.25, "days": 30}) == ["backtest lost ₹88.25 after fees over 30 days (40 trades)"]
    assert why({"trades": 40, "net_pnl": 0.0, "days": 30}) == ["backtest made ₹0.00 after fees, not above ₹0"]
    assert why({"trades": 40, "net_pnl": 99.0, "days": 30}, bt_unavailable="no stock backtests yet") == \
        ["backtest check: no stock backtests yet"]
    off = params_from(None)
    assert gates(metrics(raw("X"), off, CHARGES), off) == []                        # gate off: nothing changes


def test_run_screen_attaches_results_and_fails_closed_without_them(cfg, monkeypatch):
    from bot.instruments import load_master
    cfg = copy.deepcopy(cfg)
    cfg["screener"] = {"backtest_gate": True}
    monkeypatch.setattr(screen_data, "collect", lambda broker, insts, now, lb, pause=1.0, progress=None:
                        ([raw(i.symbol) for i in insts], []))
    master = load_master(FIXTURE_MASTER)
    snap = screen_data.run_screen(cfg, None, master, NOW, ["SBIN-EQ", "INFY-EQ"])
    assert snap["eligible"] == 0 and all("backtest check: no stock backtests yet" in r["excluded"][0] for r in snap["rows"])
    assert any("Every stock is left out" in e for e in snap["errors"])
    SB.save({"run_at": EVENING.isoformat(), "fingerprint": SB.fingerprint(cfg),
             "results": {"SBIN-EQ": {"trades": 30, "net_pnl": 75.0, "days": 30},
                         "INFY-EQ": {"trades": 30, "net_pnl": -40.0, "days": 30}}})
    snap = screen_data.run_screen(cfg, None, master, NOW, ["SBIN-EQ", "INFY-EQ"])
    assert snap["picks"] == ["SBIN-EQ"]
    infy = next(r for r in snap["rows"] if r["symbol"] == "INFY-EQ")
    assert infy["excluded"] == ["backtest lost ₹40.00 after fees over 30 days (30 trades)"]
    page, msg = render_html(snap), summary_html(snap)
    assert "Backtest 30d" in page and "+₹75.00" in page and "Backtest check:" in page
    assert "backtest +₹75.00 in 30 trades" in msg


def test_backtest_check_toggle_in_telegram():
    on, off = (ST.BY_ID["btgate"]["choices"].index(v) for v in (True, False))
    base = bool(ST.load()["screener"].get("backtest_gate"))
    first, second = (off, on) if base else (on, off)
    text, _ = callback(f"cfgv:btgate:{first}")
    assert "Saved" in text and ST.load()["screener"]["backtest_gate"] is (not base)
    assert ST.overrides() == {"screener.backtest_gate": not base}
    text, _ = callback(f"cfgv:btgate:{second}")
    assert "Saved" in text and ST.overrides() == {} and bool(ST.load()["screener"].get("backtest_gate")) is base


def test_cli_refuses_while_the_bot_runs(monkeypatch):
    from bot import cli
    control.write_status({"state": "running", "replay": False})
    with pytest.raises(SystemExit) as e:
        cli.main(["backtest-stocks"])
    assert "bot is running" in str(e.value)
