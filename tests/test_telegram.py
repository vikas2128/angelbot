from bot import control
from bot.telegram_ctl import TelegramController, handle


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
    monkeypatch.setattr(t, "send", lambda text: sent.append(text))
    assert t.poll_once() == 2
    assert len(sent) == 1 and "/status" in sent[0]       # only the authorised chat got a reply
    assert control.read_control()["kill"] is False        # stranger's /kill ignored
    assert t.offset == 7
