from datetime import datetime, timedelta

import pytest

from bot import control, selection
from bot.models import Instrument
from bot.timeutil import IST

DAY = "2026-10-22"
CONFIGURED = [Instrument("SBIN-EQ", "3045", "NSE", 1, 0.05, 1, "INTRADAY")]
MASTER = [{"symbol": s, "token": t, "exch_seg": "NSE", "lotsize": "1", "tick_size": "5"}
          for s, t in (("SBIN-EQ", "3045"), ("TATASTEEL-EQ", "3499"), ("HDFCBANK-EQ", "1333"), ("INFY-EQ", "1594"))]


class Clock:
    """A fake clock: sleeping advances it, and a hook can act at chosen moments (e.g. a Telegram tap)."""

    def __init__(self, start="09:05:00"):
        self.t = datetime.fromisoformat(f"{DAY} {start}").replace(tzinfo=IST)
        self.hooks = []                      # (time string, function)

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)
        for hhmmss, fn in list(self.hooks):
            if self.t.strftime("%H:%M:%S") >= hhmmss:
                self.hooks.remove((hhmmss, fn))
                fn()


@pytest.fixture
def files(tmp_path, monkeypatch):
    for name in ("SEL_REQUEST", "SEL_CHOICE", "SEL_STATE", "SCREEN"):
        monkeypatch.setattr(control, name, tmp_path / f"{name}.json")
    return tmp_path


def row(sym, score, gap=1.0, ltp=100.0, excluded=()):
    return {"symbol": sym, "score": score, "gap_pct": gap, "range_to_cost": 9.0, "ltp": ltp, "why": ["x"], "excluded": list(excluded),
            "rank": None if excluded else 1}


def snapshot(*rows):
    ok = [r for r in rows if not r["excluded"]]
    return {"rows": ok + [r for r in rows if r["excluded"]], "params": {"top_n": 3}, "market_date": DAY, "universe": len(rows), "eligible": len(ok)}


PICKS = snapshot(row("TATASTEEL-EQ", 85, ltp=172.0), row("HDFCBANK-EQ", 68, ltp=1710.0), row("INFY-EQ", 57, ltp=1620.0),
                 row("THIN-EQ", None, excluded=["traded value too low"]))


def cfg(**sel):
    return {"session": {"entry_start": "09:20"}, "selection": {"mode": "auto", **sel}}


def go(config, clock=None, screen=None, keep_state=False):
    """One morning's run. State files from a previous run are cleared unless keep_state (a restart on the same day)."""
    if not keep_state:
        for path in (control.SEL_STATE, control.SEL_REQUEST, control.SEL_CHOICE):
            path.unlink(missing_ok=True)
    clock = clock or Clock()
    sent = []
    insts, decision = selection.run(config, object(), CONFIGURED, MASTER, now_fn=clock.now, sleep_fn=clock.sleep,
                                    notify_fn=lambda text, buttons=None: sent.append((text, buttons)),
                                    screen_fn=screen or (lambda *a: PICKS))
    return insts, decision, sent, clock


def tap(choice, day=DAY):
    return lambda: control.write_json(control.SEL_CHOICE, {"market_date": day, "choice": choice})


def test_off_by_default_changes_nothing(files):
    insts, d, sent, _ = go({"session": {"entry_start": "09:20"}})
    assert insts == CONFIGURED and d["source"] == "off" and not sent
    assert not (files / "SEL_STATE.json").exists()


def test_invalid_settings_fall_back_and_say_so(files):
    insts, d, sent, _ = go(cfg(mode="sometimes"))
    assert insts == CONFIGURED and "selection.mode" in sent[0][0]
    with pytest.raises(ValueError, match="confirm_until <= session.entry_start"):
        selection.settings(cfg(confirm_until="09:25:00"))
    with pytest.raises(ValueError, match="top_n"):
        selection.settings(cfg(top_n=9))


def test_auto_waits_for_the_open_then_takes_the_top_pick(files):
    insts, d, sent, clock = go(cfg(top_n=1))
    assert [i.symbol for i in insts] == ["TATASTEEL-EQ"] and insts[0].order_qty == 1 and insts[0].token == "3499"
    assert clock.t.strftime("%H:%M:%S") >= "09:16:30"                      # waited until after the open
    assert d["source"] == "auto" and control.read_json(control.SEL_STATE)["symbols"] == ["TATASTEEL-EQ"]
    assert "Trading today: TATASTEEL-EQ" in sent[-1][0]
    assert [i.symbol for i in go(cfg(top_n=2))[0]] == ["TATASTEEL-EQ", "HDFCBANK-EQ"]


def test_order_value_sets_the_quantity(files):
    insts, *_ = go(cfg(top_n=1, order_value=40000))
    assert insts[0].order_qty == 232                                       # 40000 // 172


def test_confirm_uses_your_tap_and_stops_waiting(files):
    clock = Clock()
    clock.hooks.append(("09:17:30", tap(["HDFCBANK-EQ"])))
    insts, d, sent, clock = go(cfg(mode="confirm", top_n=2), clock)
    assert [i.symbol for i in insts] == ["HDFCBANK-EQ"] and d["source"] == "you"
    assert clock.t.strftime("%H:%M:%S") < "09:19:30"                       # did not sit out the whole window
    prompt, buttons = sent[0]
    assert "Today's picks" in prompt and "TATASTEEL-EQ" in prompt and "not backtested" in prompt
    assert any(b["callback_data"] == "sel:top" for r in buttons for b in r) and buttons[-1][0]["callback_data"] == "sel:config"
    req = control.read_json(control.SEL_REQUEST)
    assert req["phase"] == "decided" and req["decided"] == ["HDFCBANK-EQ"]


def test_confirm_no_tap_follows_on_timeout(files):
    insts, d, sent, clock = go(cfg(mode="confirm"))
    assert insts == CONFIGURED and d["source"] == "timeout" and clock.t.strftime("%H:%M:%S") >= "09:19:30"
    insts, d, *_ = go(cfg(mode="confirm", on_timeout="top", top_n=2))
    assert [i.symbol for i in insts] == ["TATASTEEL-EQ", "HDFCBANK-EQ"] and d["source"] == "timeout"


def test_confirm_keep_my_list_button(files):
    clock = Clock()
    clock.hooks.append(("09:17:00", tap("config")))
    insts, d, *_ = go(cfg(mode="confirm", on_timeout="top"), clock)
    assert insts == CONFIGURED and d["source"] == "you"


def test_a_tampered_or_stale_choice_is_ignored(files):
    for bad in (tap(["EVIL-EQ"]), tap(["THIN-EQ"]), tap(["HDFCBANK-EQ"], day="2026-10-21"), tap([])):
        clock = Clock()
        clock.hooks.append(("09:17:00", bad))
        insts, d, *_ = go(cfg(mode="confirm"), clock)
        assert insts == CONFIGURED and d["source"] == "timeout"             # fell to the default, not the bad choice


def test_nothing_eligible_or_a_failed_screen_falls_back(files):
    empty = lambda *a: snapshot(row("THIN-EQ", None, excluded=["too thin"]))
    insts, d, sent, _ = go(cfg(), screen=empty)
    assert insts == CONFIGURED and d["source"] == "config" and "no stock passed" in d["note"]
    insts, d, sent, _ = go(cfg(on_failure="none"), screen=empty)
    assert insts == [] and d["source"] == "none" and "No trading today" in sent[-1][0]

    def boom(*a):
        raise RuntimeError("quotes down")
    insts, d, *_ = go(cfg(), screen=boom)
    assert insts == CONFIGURED and "quotes down" in d["note"]
    insts, d, *_ = go(cfg(on_failure="none"), screen=boom)
    assert insts == []


def test_a_restart_keeps_todays_stocks_without_screening_again(files):
    go(cfg(top_n=1))
    clock = Clock("11:30:00")

    def must_not_run(*a):
        raise AssertionError("screened again after a restart")
    insts, d, sent, clock = go(cfg(top_n=1), clock, screen=must_not_run, keep_state=True)
    assert [i.symbol for i in insts] == ["TATASTEEL-EQ"] and not sent and clock.t.strftime("%H:%M") == "11:30"
    control.write_json(control.SEL_STATE, {"market_date": "2026-10-21", "symbols": ["INFY-EQ"]})   # yesterday's: not reused
    insts, d, *_ = go(cfg(top_n=1), keep_state=True)
    assert [i.symbol for i in insts] == ["TATASTEEL-EQ"]


def test_cli_hook_is_a_no_op_when_off():
    from bot.cli import _select_instruments
    import logging
    assert _select_instruments({"selection": {"mode": "off"}}, object(), CONFIGURED, logging.getLogger("t")) is CONFIGURED
    assert _select_instruments({}, object(), CONFIGURED, logging.getLogger("t")) is CONFIGURED
