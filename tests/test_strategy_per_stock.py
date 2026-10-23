"""Up to 5 stocks at once, each with the strategy that passed its walk-forward check."""
import copy
import queue
from datetime import datetime, timedelta

import pytest

from bot import control, messages as M
from bot import selection
from bot import stock_backtest as SB
from bot.engine import Engine
from bot.journal import Journal
from bot.models import Instrument
from bot.paper import PaperBroker
from bot.screener import gates, metrics, params_from
from bot.timeutil import IST
from conftest import FIXTURE_MASTER, make_bars
from test_screener import CHARGES, raw


def summ(trades, net):
    return {"trades": trades, "net_pnl": net, "charges": 1.0, "win_rate_pct": 50.0, "profit_factor": 1.2,
            "max_drawdown": -5.0, "days": 10}


def test_walk_forward_split_keeps_the_newest_third_back():
    bars = []
    for d in range(9):
        bars += make_bars([800 + i * 0.1 for i in range(5)], start=f"2026-10-{10 + d:02d} 09:15")
    train, test = SB.split_walk_forward(bars)
    tr_days = sorted({b.start.date() for b in train}); te_days = sorted({b.start.date() for b in test})
    assert len(tr_days) == 6 and len(te_days) == 3 and tr_days[-1] < te_days[0]
    assert SB.split_walk_forward(bars[:25]) == ([], [])                       # 5 days: too few to split


def test_choose_picks_on_older_days_and_confirms_on_newer():
    walk = {"ema_cross": {"train": summ(30, 120.0), "test": summ(10, 15.0)},
            "orb": {"train": summ(20, 300.0), "test": summ(8, -40.0)},           # best on older days, lost on newer
            "vwap_revert": {"train": summ(5, 500.0), "test": summ(3, 50.0)}}      # too few trades to choose on
    c = SB.choose(walk)
    assert c["strategy"] is None and "orb was best" in c["why"] and "lost" in c["why"]
    del walk["orb"]
    c = SB.choose(walk)
    assert c["strategy"] == "ema_cross" and c["test"]["net_pnl"] == 15.0
    walk["ema_cross"]["test"] = summ(2, 15.0)
    assert "traded only 2 times" in SB.choose(walk)["why"]
    assert "no strategy made money" in SB.choose({"orb": {"train": summ(30, -1.0), "test": summ(9, 5.0)}})["why"]
    assert SB.choose({})["strategy"] is None


def test_nightly_run_records_a_choice_per_stock(cfg):
    from test_stock_backtest import EVENING, candles, inst
    snap = SB.run_all(cfg, None, [inst("A-EQ")], EVENING, fetch_fn=lambda *a: candles(days=9))
    assert set(snap["choices"]["A-EQ"]) >= {"strategy"}
    assert SB.load_choices(cfg, EVENING + timedelta(hours=16))[0] is None      # not saved yet
    SB.save(snap)
    got, why = SB.load_choices(cfg, EVENING + timedelta(hours=16))
    assert why == "" and "A-EQ" in got
    switched = copy.deepcopy(cfg)
    switched["strategy"]["name"] = "orb"                                       # a default switch keeps them valid
    assert SB.load_choices(switched, EVENING + timedelta(hours=16))[1] == ""
    SB.path().unlink()


def test_screener_with_strategy_per_stock():
    p = params_from({"screener": {"strategy_per_stock": True}})
    ok = metrics(raw("A-EQ", choice={"strategy": "orb", "test": summ(8, 20.0)}), p, CHARGES)
    assert gates(ok, p) == [] and ok["strategy"] == "orb"
    no = metrics(raw("B-EQ", choice={"strategy": None, "why": "no strategy made money (with enough trades) on the older days"}), p, CHARGES)
    assert gates(no, p) == ["no strategy passed the check: no strategy made money (with enough trades) on the older days"]
    assert gates(metrics(raw("C-EQ"), p, CHARGES), p) == ["no recent strategy check for this stock"]
    assert gates(metrics(raw("D-EQ"), {**p, "bt_unavailable": "no per-stock strategy check yet"}, CHARGES),
                 {**p, "bt_unavailable": "no per-stock strategy check yet"})[0].startswith("strategy check:")


@pytest.fixture
def clean_sel():
    for f in (control.SEL_STATE, control.SEL_REQUEST, control.SEL_CHOICE, control.SCREEN):
        f.unlink(missing_ok=True)
    yield
    for f in (control.SEL_STATE, control.SEL_REQUEST, control.SEL_CHOICE, control.SCREEN):
        f.unlink(missing_ok=True)


def test_selection_carries_each_picks_strategy_and_keeps_it_after_a_restart(cfg, clean_sel):
    from bot.instruments import load_master
    master = load_master(FIXTURE_MASTER)
    cfg = copy.deepcopy(cfg)
    cfg["selection"] = {"mode": "auto", "top_n": 5, "screen_at": "09:16:30", "confirm_until": "09:19:30"}
    now = datetime(2026, 10, 23, 9, 17, tzinfo=IST)
    rows = [{"symbol": "INFY-EQ", "excluded": [], "strategy": "orb", "ltp": 1500.0},
            {"symbol": "SBIN-EQ", "excluded": [], "strategy": "vwap_revert", "ltp": 800.0},
            {"symbol": "HDFCBANK-EQ", "excluded": [], "strategy": None, "ltp": 1600.0}]
    config_insts = [Instrument("SBIN-EQ", "3045", "NSE", 1, 0.05, 5, "INTRADAY")]
    sent = []
    insts, d = selection.run(cfg, None, config_insts, master, now_fn=lambda: now, sleep_fn=lambda s: None,
                             notify_fn=lambda t, b=None: sent.append(t), screen_fn=lambda *a: {"rows": rows, "params": {}})
    assert [(i.symbol, i.strategy) for i in insts] == [("INFY-EQ", "orb"), ("SBIN-EQ", "vwap_revert"), ("HDFCBANK-EQ", None)]
    assert insts[1].qty == 5                                                  # a configured stock keeps its settings
    assert d["strategies"] == {"INFY-EQ": "orb", "SBIN-EQ": "vwap_revert"}
    assert "Strategy per stock" in sent[-1] and "INFY-EQ: opening range breakout" in sent[-1]
    control.SCREEN.unlink(missing_ok=True)                                     # even without this morning's snapshot
    again, _ = selection.run(cfg, None, config_insts, master, now_fn=lambda: now, sleep_fn=lambda s: None,
                             notify_fn=lambda t, b=None: None)
    assert [(i.symbol, i.strategy) for i in again] == [("INFY-EQ", "orb"), ("SBIN-EQ", "vwap_revert"), ("HDFCBANK-EQ", None)]


def test_engine_runs_each_stock_with_its_own_strategy(cfg, tmp_path, monkeypatch):
    from bot import notify
    from test_trading import _FakeFeed
    monkeypatch.setattr(notify, "send", lambda t, b=None: None)
    insts = [Instrument("SBIN-EQ", "3045", "NSE", 1, 0.05, 1, "INTRADAY", strategy="orb"),
             Instrument("INFY-EQ", "1594", "NSE", 1, 0.05, 1, "INTRADAY")]
    eng = Engine(cfg, PaperBroker(0.0), _FakeFeed(), insts, Journal(tmp_path / "j.db"), queue.Queue())
    assert {s: x.name for s, x in eng.strategies.items()} == {"SBIN-EQ": "orb", "INFY-EQ": cfg["strategy"]["name"]}
    assert eng.om.strategy_of("SBIN-EQ") == "orb" and eng.om.strategy_of("INFY-EQ") == cfg["strategy"]["name"]
    eng._write_status(0.0)
    s = control.read_status()
    assert s["strategies"]["SBIN-EQ"].startswith("orb(") and s["strategy_state"]["SBIN-EQ"]["strategy"] == "orb"
    assert "a strategy per stock" in M.status_card(s, {"paused": False, "kill": False})


def test_five_positions_and_picks_are_allowed_and_six_is_not(cfg):
    c = copy.deepcopy(cfg)
    c["selection"] = {"mode": "auto", "top_n": 5}
    assert selection.settings(c)["top_n"] == 5
    c["selection"]["top_n"] = 6
    with pytest.raises(ValueError, match="between 1 and 5"):
        selection.settings(c)
    from bot import settings as ST
    assert ST.BY_ID["topn"]["choices"][-1] == 5 and ST.BY_ID["maxpos"]["choices"] == [1, 2, 3, 4, 5]
    assert ST.BY_ID["perstock"]["path"] == "screener.strategy_per_stock"
