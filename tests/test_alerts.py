import json

from bot import alerts, notify


def setup_function(_):
    alerts.path().unlink(missing_ok=True)


def test_every_alert_goes_to_the_outbox_even_without_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    notify.send("🟢 <b>Bought SBIN-EQ</b>\n5 shares at ₹966.20")
    recs = alerts.read()
    assert len(recs) == 1 and recs[0]["html"].startswith("🟢 <b>Bought")
    assert recs[0]["text"] == "🟢 Bought SBIN-EQ\n5 shares at ₹966.20" and "TEST" in recs[0]["label"]


def test_muted_replays_write_nothing():
    notify.set_muted(True)
    try:
        notify.send("per-trade alert during a replay")
    finally:
        notify.set_muted(False)
    assert alerts.read() == []


def test_read_since_limit_and_bad_lines():
    for i in range(5):
        alerts.append(f"a{i}", f"a{i}", "PAPER")
    with open(alerts.path(), "a") as f:
        f.write("not json\n")
    recs = alerts.read()
    assert [r["text"] for r in recs] == ["a0", "a1", "a2", "a3", "a4"]
    assert [r["text"] for r in alerts.read(limit=2)] == ["a3", "a4"]
    assert alerts.read(since="9999") == [] and len(alerts.read(since="0000")) == 5


def test_outbox_stays_small(monkeypatch):
    monkeypatch.setattr(alerts, "MAX_BYTES", 2000)
    monkeypatch.setattr(alerts, "KEEP_LINES", 10)
    for i in range(60):
        alerts.append(f"alert number {i}", f"alert number {i}", "PAPER")
    lines = alerts.path().read_text().splitlines()
    assert len(lines) <= 30 and json.loads(lines[-1])["text"] == "alert number 59"


def test_a_write_failure_never_reaches_the_bot(monkeypatch):
    monkeypatch.setattr(alerts, "path", lambda: (_ for _ in ()).throw(PermissionError("read-only disk")))
    alerts.append("x", "x", "PAPER")                                     # logs a warning, does not raise
    notify.send("still fine")
