from bot import control, messages as M
from datetime import datetime

import pytest

from bot.journal import Journal
from bot.telegram_ctl import TelegramController, build_report_file, callback, handle, route
from bot.timeutil import IST


def test_help_and_unknown():
    assert "/status" in handle("/help")
    assert "Unknown command" in handle("/buy SBIN 100")


def test_pause_resume_kill_confirm():
    assert "Paused" in handle("/pause")
    assert control.read_control()["paused"] is True
    assert "Resumed" in handle("/resume@AngelBot_bot")
    c = control.read_control()
    assert c["paused"] is False and c["kill"] is False
    assert "/kill CONFIRM" in handle("/kill")            # needs confirmation
    assert control.read_control()["kill"] is False
    assert "Kill switch set" in handle("/kill CONFIRM")
    assert control.read_control()["kill"] is True
    handle("/resume")
    assert control.read_control()["kill"] is False


def test_status_pnl_log_never_crash():
    for cmd in ("/status", "/pnl", "/log"):
        assert isinstance(handle(cmd), str)


def test_ignores_unauthorised_chat(monkeypatch):
    t = TelegramController("TOKEN", "111")
    sent = []
    updates = [{"update_id": 5, "message": {"chat": {"id": 999}, "text": "/kill CONFIRM"}},
               {"update_id": 6, "message": {"chat": {"id": 111}, "text": "/help"}}]
    monkeypatch.setattr(t, "_api", lambda method, **p: updates if method == "getUpdates" else None)
    monkeypatch.setattr(t, "send", lambda text, markup=None: sent.append(text))
    assert t.poll_once() == 2
    assert len(sent) == 1 and "/status" in sent[0]       # only the authorised chat got a reply
    assert control.read_control()["kill"] is False        # stranger's /kill ignored
    assert t.offset == 7


def _closed(net, reason="target"):
    return {"symbol": "SBIN-EQ", "net_pnl": net, "charges": 2.0, "exit_reason": reason, "exit_ts": "2026-10-21T10:05:00+05:30"}


def test_trade_cards_plain_english_and_escaped():
    opened = M.opened("SBIN-EQ", "LONG", 1, 812.4, 809.96, 817.28, "ema_cross", "paper")
    assert "Bought SBIN-EQ" in opened and "Safety exit" in opened
    done = M.closed("SBIN-EQ", "LONG", 1, 812.4, 817.28, 4.88, 2.28, 2.6, "target", "paper")
    assert "profit" in done and "+₹2.60" in done and "Goal reached" in done
    assert "loss" in M.closed("X", "LONG", 1, 10, 9, -1, 1, -2, "stop_loss", "live")
    from datetime import datetime
    from bot.timeutil import IST
    t1 = datetime(2026, 10, 21, 10, 5, 32, tzinfo=IST)
    assert "🕐 21 Oct, 10:05:32 IST" in M.opened("SBIN-EQ", "LONG", 1, 812.4, 809.96, 817.28, "orb", "paper", t1)
    out = M.closed("SBIN-EQ", "LONG", 1, 812.4, 817.28, 4.88, 2.28, 2.6, "target", "paper", t1, t1)
    assert "In 21 Oct, 10:05:32 IST" in out and "Out 21 Oct, 10:05:32 IST" in out
    assert "&lt;b&gt;" in M.order_problem("BUY", 1, "SBIN-EQ", "<b>bad</b>")      # broker text escaped


def test_daily_summary():
    text = M.daily_summary([_closed(6.1), _closed(-3.4, "stop_loss")], "2026-10-21", "paper")
    assert "+₹2.70" in text and "Won 1 · Lost 1" in text and "Safety exit 1" in text
    assert "No trades" in M.daily_summary([], "2026-10-21", "paper")


def test_menu_and_buttons():
    text, markup = route("/menu")
    assert markup and any(b["callback_data"] == "kill_ask" for row in markup for b in row)
    assert isinstance(callback("status")[0], str) and isinstance(callback("summary")[0], str)
    assert "Paused" in callback("pause")[0] and control.read_control()["paused"] is True
    assert "Resumed" in callback("resume")[0]


def test_kill_button_needs_fresh_confirmation():
    text, markup = callback("kill_ask")
    data = markup[0][0]["callback_data"]
    assert data.startswith("kill_do:") and control.read_control()["kill"] is False
    stale = callback(data, now=int(data.split(":")[1]) + 3600)
    assert "expired" in stale[0] and control.read_control()["kill"] is False
    assert "Kill switch set" in callback(data)[0] and control.read_control()["kill"] is True
    handle("/resume")


def test_ignores_unauthorised_button_press(monkeypatch):
    t = TelegramController("TOKEN", "111")
    calls = []
    cq = {"id": "1", "data": "kill_do:%d" % 10**10, "message": {"message_id": 3, "chat": {"id": 999}}}
    monkeypatch.setattr(t, "_api", lambda method, **p: [{"update_id": 9, "callback_query": cq}]
                        if method == "getUpdates" else calls.append(method))
    t.poll_once()
    assert calls == ["answerCallbackQuery"] and control.read_control()["kill"] is False


def test_alert_label_live_vs_test(monkeypatch):
    from bot import notify
    monkeypatch.setattr(notify, "_mode", "live")
    assert "TEST" in notify.label() and "unit test" in notify.label()      # pytest can never look LIVE
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert "LIVE" in notify.label()
    monkeypatch.setattr(notify, "_mode", "paper")
    assert "PAPER" in notify.label() and "LIVE" not in notify.label()
    monkeypatch.setattr(notify, "_mode", None)
    assert "PAPER" in notify.label()


def test_send_prepends_label(monkeypatch):
    from bot import notify
    seen = {}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: seen.update(json) or type("R", (), {"status_code": 200})())
    monkeypatch.setattr(notify.threading, "Thread", lambda target, daemon: type("T", (), {"start": staticmethod(target)})())
    notify.send("hello")
    assert seen["text"].startswith("🧪 <b>TEST</b> · unit test") and seen["text"].endswith("hello")


def _journal(path):
    j = Journal(path)
    t = lambda d, h: datetime.fromisoformat(f"{d} {h}").replace(tzinfo=IST)
    j.event(t("2026-10-22", "09:05"), "start", "mode=paper replay=False")
    j.trade("paper", "SBIN-EQ", "LONG", 1, t("2026-10-22", "10:00"), 800, t("2026-10-22", "10:05"), 804.8, 4.8, 2.2, "target", "ema_cross")
    j.trade("paper", "SBIN-EQ", "LONG", 1, t("2026-10-21", "10:00"), 800, t("2026-10-21", "10:05"), 797, -3, 2.2, "stop_loss", "ema_cross")
    return j


def _pin_today(monkeypatch, day="2026-10-22"):
    monkeypatch.setattr("bot.telegram_ctl.now_ist", lambda: datetime.fromisoformat(f"{day} 12:00").replace(tzinfo=IST))


def test_report_file_built_from_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "JOURNAL", tmp_path / "j.db")
    _pin_today(monkeypatch)
    _journal(tmp_path / "j.db")
    out = tmp_path / "out"; out.mkdir()
    path, caption = build_report_file("", out)
    page = path.read_text()
    assert path.name == "angelbot-report-2026-10-22.html" and 'id="today"' in page and 'id="month"' in page
    assert "Today</b>: 1 trade," in caption and "October 2026</b>: 2 trades" in caption
    assert "Under 100 trades" in caption and "PAPER" in caption and "Tabs: Today and Month" in caption
    path2, caption2 = build_report_file("session 2026-10-22", out)          # month tab restarts on the date given
    assert "Since Oct 22</b>: 1 trade," in caption2
    path3, caption3 = build_report_file("replay", out)
    assert "Today</b>: no closed trades yet" in caption3                    # the 22nd's trade is a real session


def test_report_errors_are_friendly(tmp_path, monkeypatch):
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(control, "JOURNAL", tmp_path / "missing.db")
    with pytest.raises(ValueError, match="No journal"):
        build_report_file("", out)
    monkeypatch.setattr(control, "JOURNAL", tmp_path / "j.db")
    _journal(tmp_path / "j.db")
    with pytest.raises(ValueError, match="Unknown option"):
        build_report_file("banana", out)
    _pin_today(monkeypatch)
    with pytest.raises(ValueError, match="No closed trades"):
        build_report_file("2030-01-01", out)


def test_report_command_only_for_authorised_chat(monkeypatch):
    t = TelegramController("TOKEN", "111")
    asked = []
    updates = [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/report"}},
               {"update_id": 2, "message": {"chat": {"id": 111}, "text": "/report@AngelBot_bot session 2026-10-01"}}]
    monkeypatch.setattr(t, "_api", lambda method, **p: updates if method == "getUpdates" else None)
    monkeypatch.setattr(t, "send_report", lambda arg="": asked.append(arg))
    monkeypatch.setattr(t, "send", lambda text, markup=None: None)
    t.poll_once()
    assert asked == ["session 2026-10-01"]


def test_report_button_only_for_authorised_chat(monkeypatch):
    t = TelegramController("TOKEN", "111")
    asked = []
    monkeypatch.setattr(t, "_api", lambda method, **p: None)
    monkeypatch.setattr(t, "send_report", lambda arg="": asked.append(arg))
    t._handle_callback({"id": "1", "data": "report", "message": {"message_id": 3, "chat": {"id": 999}}})
    assert asked == []
    t._handle_callback({"id": "2", "data": "report", "message": {"message_id": 3, "chat": {"id": 111}}})
    assert asked == [""]
    assert any(b["callback_data"] == "report" for row in route("/menu")[1] for b in row)


def test_send_document_uploads_the_file(tmp_path):
    f = tmp_path / "r.html"; f.write_text("<html>hi</html>")
    calls = []

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    class Sess:
        def post(self, url, data=None, files=None, timeout=None):
            calls.append((url, data, files["document"][0], files["document"][1].read()))
            return Resp()

    t = TelegramController("TOKEN", "111")
    t.s = Sess()
    t.send_document(f, "📄 <b>caption</b>")
    url, data, name, body = calls[0]
    assert url.endswith("/sendDocument") and data["chat_id"] == "111" and data["parse_mode"] == "HTML"
    assert name == "r.html" and body == b"<html>hi</html>"


def test_send_report_reports_problems_in_chat(monkeypatch):
    t = TelegramController("TOKEN", "111")
    sent = []
    monkeypatch.setattr(t, "send", lambda text, markup=None: sent.append(text))
    monkeypatch.setattr("bot.telegram_ctl.build_report_file", lambda arg, out: (_ for _ in ()).throw(ValueError("No journal yet")))
    t.send_report("")
    assert sent == ["📄 No journal yet"]
    monkeypatch.setattr("bot.telegram_ctl.build_report_file", lambda arg, out: (_ for _ in ()).throw(RuntimeError("boom")))
    t.send_report("")
    assert "Couldn't build the report" in sent[-1]


def _write_snapshot(tmp_path, monkeypatch, day="2026-10-22"):
    from bot import screener
    import test_screener as ts
    monkeypatch.setattr(control, "SCREEN", tmp_path / "screen.json")
    now = datetime.fromisoformat(f"{day} 09:17").replace(tzinfo=IST)
    rows = [ts.raw("HOT", open=1025.0, volume=900_000), ts.raw("MID", open=1008.0, volume=300_000)]
    control.write_screen(screener.rank(rows, screener.params_from(None), ts.CHARGES, now))


def test_screen_file_from_the_latest_snapshot(tmp_path, monkeypatch):
    from bot.telegram_ctl import build_screen_file
    monkeypatch.setattr(control, "SCREEN", tmp_path / "none.json")
    with pytest.raises(ValueError, match="No screener snapshot"):
        build_screen_file(tmp_path)
    _write_snapshot(tmp_path, monkeypatch)
    monkeypatch.setattr("bot.telegram_ctl.now_ist", lambda: datetime.fromisoformat("2026-10-22 09:30").replace(tzinfo=IST))
    out = tmp_path / "out"; out.mkdir()
    path, text = build_screen_file(out)
    assert path.name == "angelbot-screener-2026-10-22.html" and "Stock screener" in path.read_text()
    assert "HOT" in text and "Advisory only" in text and "not today" not in text
    monkeypatch.setattr("bot.telegram_ctl.now_ist", lambda: datetime.fromisoformat("2026-10-23 09:30").replace(tzinfo=IST))
    assert "not today" in build_screen_file(out)[1]


def test_screen_command_and_button_only_for_the_authorised_chat(monkeypatch):
    t = TelegramController("TOKEN", "111")
    asked = []
    updates = [{"update_id": 1, "message": {"chat": {"id": 999}, "text": "/screen"}},
               {"update_id": 2, "message": {"chat": {"id": 111}, "text": "/screen@AngelBot_bot"}}]
    monkeypatch.setattr(t, "_api", lambda method, **p: updates if method == "getUpdates" else None)
    monkeypatch.setattr(t, "send_screen", lambda: asked.append("cmd"))
    monkeypatch.setattr(t, "send", lambda text, markup=None: None)
    t.poll_once()
    t._handle_callback({"id": "1", "data": "screen", "message": {"message_id": 3, "chat": {"id": 999}}})
    t._handle_callback({"id": "2", "data": "screen", "message": {"message_id": 3, "chat": {"id": 111}}})
    assert asked == ["cmd", "cmd"]
    assert any(b["callback_data"] == "screen" for row in route("/menu")[1] for b in row)
    assert "/screen" in handle("/help")


def test_send_screen_reports_a_missing_snapshot_in_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "SCREEN", tmp_path / "none.json")
    t = TelegramController("TOKEN", "111")
    sent = []
    monkeypatch.setattr(t, "send", lambda text, markup=None: sent.append(text))
    t.send_screen()
    assert sent and "No screener snapshot yet" in sent[0] and "./deploy/ab paper screen" in sent[0]


def _request(tmp_path, monkeypatch, phase="waiting", day="2026-10-22"):
    monkeypatch.setattr(control, "SEL_REQUEST", tmp_path / "req.json")
    monkeypatch.setattr(control, "SEL_CHOICE", tmp_path / "choice.json")
    control.write_json(control.SEL_REQUEST, {"market_date": day, "phase": phase, "decided": ["HDFCBANK-EQ"],
                                             "top": ["TATASTEEL-EQ", "HDFCBANK-EQ"],
                                             "picks": [{"symbol": "TATASTEEL-EQ"}, {"symbol": "HDFCBANK-EQ"}, {"symbol": "INFY-EQ"}]})


def test_tapping_a_pick_writes_the_choice(tmp_path, monkeypatch):
    from bot.telegram_ctl import apply_choice
    _request(tmp_path, monkeypatch)
    assert "Got it" in apply_choice("HDFCBANK-EQ", "2026-10-22")
    assert control.read_json(control.SEL_CHOICE)["choice"] == ["HDFCBANK-EQ"]
    apply_choice("top", "2026-10-22")
    assert control.read_json(control.SEL_CHOICE)["choice"] == ["TATASTEEL-EQ", "HDFCBANK-EQ"]
    apply_choice("config", "2026-10-22")
    assert control.read_json(control.SEL_CHOICE)["choice"] == "config"


def test_a_tap_cannot_add_an_unlisted_stock_or_change_a_decision(tmp_path, monkeypatch):
    from bot.telegram_ctl import apply_choice
    _request(tmp_path, monkeypatch)
    assert "not one of today's picks" in apply_choice("EVIL-EQ", "2026-10-22")
    assert not (tmp_path / "choice.json").exists()
    _request(tmp_path, monkeypatch, phase="decided")
    assert "Too late" in apply_choice("INFY-EQ", "2026-10-22") and not (tmp_path / "choice.json").exists()
    _request(tmp_path, monkeypatch, day="2026-10-21")
    assert "No stock selection is open" in apply_choice("INFY-EQ", "2026-10-22")


def test_selection_button_is_routed_by_the_callback(tmp_path, monkeypatch):
    _request(tmp_path, monkeypatch)
    monkeypatch.setattr("bot.telegram_ctl.now_ist", lambda: datetime.fromisoformat("2026-10-22 09:17").replace(tzinfo=IST))
    text, markup = callback("sel:top")
    assert "Got it" in text and markup is None
