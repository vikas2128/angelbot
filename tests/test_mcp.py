import importlib


def test_mcp_tools_guardrails(tmp_path):
    srv = importlib.import_module("mcp_server.server")
    assert "error" in srv.journal_query("DELETE FROM trades")
    assert "error" in srv.journal_query("SELECT 1; DROP TABLE trades")
    assert srv.kill_switch("yes")["ok"] is False
    assert srv.validate_config()["valid"] is True
    assert "ema_cross" in srv.list_strategies()
    p = srv.pause_bot("t")
    assert p["paused"] is True
    r = srv.resume_bot()
    assert r["paused"] is False and r["kill"] is False
    assert not hasattr(srv, "place_order")
    assert "error" in srv.trades(date="2026-10-14' OR 1=1 --")


def test_service_status_checks_this_areas_units(monkeypatch):
    from types import SimpleNamespace
    srv = importlib.import_module("mcp_server.server")
    monkeypatch.setenv("ANGELBOT_PROFILE", "paper")
    states = {"angelbot@paper.service": "active", "angelbot@paper.timer": "active",
              "angelbot-telegram@paper.service": "active", "angelbot-backtest@paper.timer": "active",
              "angelbot-backtest@paper.service": "inactive", "angelbot.service": "failed",
              "angelbot.timer": "inactive", "angelbot-telegram.service": "inactive"}
    asked = []

    def fake(cmd, **kw):
        asked.append(cmd)
        return SimpleNamespace(stdout=states.get(cmd[-1], "NEXT LEFT ...") if cmd[2] == "is-active" else "timers", stderr="")
    monkeypatch.setattr(srv, "_SYSTEMCTL", fake)
    out = srv.service_status()
    assert out["area"] == "paper" and out["angelbot@paper.service"] == "active"
    assert out["angelbot-telegram@paper.service"] == "active" and out["angelbot-backtest@paper.timer"] == "active"
    assert out["legacy_units"]["angelbot.service"] == "failed" and "angelbot.timer" not in out["legacy_units"]
    assert out["timers"] == "timers" and ["systemctl", "--user", "is-active", "angelbot.service"] in asked
    states["angelbot.service"] = "inactive"
    assert "legacy_units" not in srv.service_status()
