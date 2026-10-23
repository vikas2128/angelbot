from types import SimpleNamespace

import pytest
import yaml

from bot import config as C
from bot import control, settings as ST
from bot import telegram_ctl as T
from bot.telegram_ctl import callback, route


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for p in (ST.path(), control.STATUS):
        p.unlink(missing_ok=True)
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    yield
    for p in (ST.path(), control.STATUS):
        p.unlink(missing_ok=True)


def idx(sid, value):
    return ST.BY_ID[sid]["choices"].index(value)


def test_merge_sets_paths_and_every_instrument_qty():
    base = C.load_config()
    base["instruments"] = [{"symbol": "A-EQ", "exchange": "NSE", "qty": 1}, {"symbol": "B-EQ", "exchange": "NSE", "qty": 3}]
    m = ST.merge(base, {"selection.qty": 5, "instruments.qty": 2, "screener.universe": ["nifty50"], "bogus.key": 1})
    assert m["selection"]["qty"] == 5 and [i["qty"] for i in m["instruments"]] == [2, 2]
    assert m["screener"]["universe"] == ["nifty50"] and "bogus" not in m
    assert [i["qty"] for i in base["instruments"]] == [1, 3]                       # the base is not changed


def test_apply_saves_an_override_that_load_uses_and_back_to_base_removes_it():
    base = C.load_config()
    old, new = ST.apply("maxtr", idx("maxtr", 5))
    assert new == "5" and ST.overrides() == {"risk.max_trades_per_day": 5}
    assert ST.load()["risk"]["max_trades_per_day"] == 5
    assert C.load_config()["risk"]["max_trades_per_day"] == base["risk"]["max_trades_per_day"]   # config.yaml untouched
    ST.apply("maxtr", idx("maxtr", base["risk"]["max_trades_per_day"]))
    assert ST.overrides() == {}                                                    # same as config.yaml: no override kept
    hist = ST.read()["history"]
    assert [h["to"] for h in hist] == [5, base["risk"]["max_trades_per_day"]]


def test_invalid_result_is_refused_and_nothing_is_written():
    base = C.load_config()
    base["session"]["entry_start"] = "09:18"                     # then confirm_until 09:19:30 is after the entry start
    with pytest.raises(ST.SettingsError, match="Not saved"):
        ST.apply("selmode", idx("selmode", "confirm"), base=base)
    assert not ST.path().exists()
    with pytest.raises(ST.SettingsError):
        ST.apply("nope", 0)
    with pytest.raises(ST.SettingsError):
        ST.apply("sl", 99)


def test_reset_drops_everything():
    ST.apply("selqty", idx("selqty", 20))
    ST.apply("univ", idx("univ", ["nifty50", "banknifty"]))
    assert len(ST.summary()) == 2 and "Nifty 50 + Bank Nifty" in " ".join(ST.summary())
    assert ST.reset() == 2 and ST.overrides() == {}
    assert yaml.safe_load(ST.path().read_text())["values"] == {}


def test_settings_card_choices_and_set_through_telegram():
    text, rows = route("/settings")
    assert "Settings" in text and "Selection mode" in text and "Max daily loss" in text
    data = [b["callback_data"] for r in rows for b in r]
    assert "cfgk:selqty" in data and "cfg_reset_ask" not in data and "rst_ask" not in data   # nothing changed, bot not running
    assert all(len(d.encode()) <= 64 for d in data)
    text, rows = callback("cfgk:selqty")
    assert "Shares for a picked stock" in text and any(b["text"].startswith("✓ ") for r in rows for b in r)
    text, rows = callback(f"cfgv:selqty:{idx('selqty', 10)}")
    assert "Saved" in text and "10 shares" in text and "✏️" in text
    assert "cfg_reset_ask" in [b["callback_data"] for r in rows for b in r]
    assert "Unknown" in callback("cfgv:selqty:x")[0] and "Unknown" in callback("cfgk:zzz")[0]


def test_reset_needs_a_fresh_confirmation():
    callback(f"cfgv:maxloss:{idx('maxloss', 1000)}")
    text, rows = callback("cfg_reset_ask")
    assert "Max daily loss: ₹1,000" in text
    stamp = rows[0][0]["callback_data"].split(":")[1]
    assert "expired" in callback(f"cfg_reset_do:{stamp}", now=int(stamp) + 61)[0] and ST.overrides()
    assert "Reset 1" in callback(f"cfg_reset_do:{stamp}", now=int(stamp) + 5)[0] and ST.overrides() == {}


def _running(**kw):
    control.write_status({"state": "running", "replay": False, "positions": [], "pending_orders": [], "trades_today": 0, **kw})


def test_restart_offered_only_for_a_running_paper_bot_with_nothing_open(monkeypatch):
    assert "isn't running" in T.restart_refusal()
    _running()
    assert T.restart_refusal() is None
    assert "rst_ask" in [b["callback_data"] for r in route("/settings")[1] for b in r]
    _running(positions=[{"symbol": "SBIN-EQ", "qty": 5}])
    assert "open position" in T.restart_refusal()
    _running(pending_orders=[{"id": "1"}])
    assert "waiting order" in T.restart_refusal()
    _running()
    monkeypatch.setenv("ANGELBOT_PROFILE", "live")
    assert "only for the paper area" in T.restart_refusal()


def test_restart_confirms_rechecks_and_runs_systemctl(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "RESTART_RUNNER", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0, stderr=""))
    _running(trades_today=2)
    text, rows = callback("rst_ask")
    assert "Restart the paper bot" in text and "start again from zero" in text
    stamp = rows[0][0]["callback_data"].split(":")[1]
    assert "expired" in callback(f"rst_do:{stamp}", now=int(stamp) + 61)[0] and not calls
    _running(positions=[{"symbol": "SBIN-EQ", "qty": 5}])                  # a trade opened in between
    assert "Not restarted" in callback(f"rst_do:{stamp}", now=int(stamp) + 5)[0] and not calls
    _running()
    assert "Restarting" in callback(f"rst_do:{stamp}", now=int(stamp) + 5)[0]
    assert calls == [["systemctl", "--user", "--no-block", "restart", "angelbot@paper.service"]]


def test_restart_failure_shows_the_manual_command(monkeypatch):
    monkeypatch.setattr(T, "RESTART_RUNNER", lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="Failed to connect to bus"))
    _running()
    stamp = callback("rst_ask")[1][0][0]["callback_data"].split(":")[1]
    text = callback(f"rst_do:{stamp}", now=int(stamp) + 1)[0]
    assert "Restart failed" in text and "Failed to connect to bus" in text and "systemctl --user restart angelbot@paper" in text


def test_start_message_lists_telegram_overrides():
    from bot import messages as M
    assert "Changed in Telegram" not in M.started("paper", "ema_cross", ["SBIN-EQ"])
    assert "Changed in Telegram: Shares for a picked stock: 5 shares" in M.started("paper", "ema_cross", ["SBIN-EQ"],
                                                                                    ["Shares for a picked stock: 5 shares"])
