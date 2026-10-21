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
