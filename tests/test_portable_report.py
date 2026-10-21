import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

from bot.journal import Journal
from bot.timeutil import IST

SPEC = importlib.util.spec_from_file_location(
    "build_report", Path(__file__).resolve().parent.parent / "skills" / "angelbot-report" / "build_report.py")
br = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(br)


def row(ts, gross, charges, reason="signal", source="replay", **kw):
    r = {"mode": "paper", "symbol": "SBIN-EQ", "strategy": "ema_cross", "qty": 1, "exit_ts": ts,
         "gross_pnl": gross, "charges": charges, "net_pnl": round(gross - charges, 2),
         "exit_reason": reason, "source": source}
    r.update(kw)
    return r


ROWS = [row("2026-10-20T10:00:00+05:30", 4.8, 2.2, "target"),
        row("2026-10-20T11:00:00+05:30", -3.0, 2.2, "stop_loss"),
        row("2026-10-21T10:30:00+05:30", -1.0, 2.2)]


def test_build_figures():
    d = br.build(ROWS, "1-minute", None)
    k = d["kpis"]
    assert (k["trades"], k["wins"], k["losses"]) == (3, 1, 2)
    assert k["net"] == -5.8 and k["fees"] == 6.6 and k["gross"] == 0.8      # nets: +2.6, -5.2, -3.2
    assert k["profit_factor"] == round(2.6 / 8.4, 2)
    assert k["max_dd"] == -8.4                                              # from the +2.6 peak down to -5.8
    assert [p["cum"] for p in d["equity"]] == [2.6, -2.6, -5.8]
    assert d["meta"]["basis"] == "replay" and d["meta"]["days"] == 2
    assert {r["reason"]: r["n"] for r in d["by_reason"]} == {"signal": 1, "target": 1, "stop_loss": 1}


def test_max_drawdown_is_peak_to_trough():
    d = br.build(ROWS, "1-minute", None)
    cums = [p["cum"] for p in d["equity"]]
    peak, worst = 0.0, 0.0
    for c in cums:
        peak = max(peak, c)
        worst = min(worst, c - peak)
    assert d["kpis"]["max_dd"] == round(worst, 2)


def test_basis_and_story():
    mixed = ROWS + [row("2026-10-22T10:00:00+05:30", 1.0, 2.2, source="session")]
    d = br.build(mixed, "5-minute", [{"item": "x", "state": "ok", "detail": "y"}])
    assert d["meta"]["basis"] == "mixed" and d["meta"]["timeframe"] == "5-minute"
    assert "mixes" in d["story"]["basis_note"] and d["setup"][0]["item"] == "x"
    assert any("Fewer than 100 trades" in s for s in d["story"]["insights"])


def test_render_is_standalone_and_script_safe(tmp_path):
    evil = [row("2026-10-20T10:00:00+05:30", 1.0, 2.2, reason="</script><b>x")]
    out = tmp_path / "r.html"
    br.render(br.build(evil, "1-minute", None), out)
    page = out.read_text()
    assert "<!--@" not in page                              # every placeholder was filled
    assert "</script><b>" not in page                       # data cannot close the script tag
    assert "&lt;/script&gt;&lt;b&gt;x" in page              # and is escaped where it is shown
    urls = set(re.findall(r"https?://[^\"' )]+", page))
    assert urls <= {"http://www.w3.org/2000/svg"}           # no network resources, SVG namespace only
    payload = page.split('id="report-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(payload)["by_reason"][0]["reason"] == "</script><b>x"


def test_report_shows_its_data_without_javascript(tmp_path):
    """Phone file previews and mail clients do not run scripts: the figures must already be in the HTML."""
    out = tmp_path / "r.html"
    br.render(br.build(ROWS, "1-minute", [{"item": "Bot process", "state": "ok", "detail": "Running"}]), out)
    page = out.read_text()
    assert re.findall(r"<script[^>]*>", page) == ['<script type="application/json" id="report-data">']
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)))
    for needle in ("\u2212\u20b95.80", "Win rate", "33.3%", "Goal reached", "Safety exit (stop loss)",
                   "Bot process", "Fewer than 100 trades", "Oct 20 to Oct 21"):
        assert needle in text, needle
    assert page.count('class="ln"') == 1 and "<path" in page and page.count("<rect") >= 2   # charts are drawn


def test_indian_grouping_and_signs():
    assert br.inr(1234567.891) == "12,34,567.89" and br.inr(999.5) == "999.50" and br.inr(1000) == "1,000.00"
    assert br.money(-279.87) == "\u2212\u20b9279.87" and br.money(2.6) == "+\u20b92.60" and br.money(0) == "\u20b90.00"


def test_reads_the_journal_file(tmp_path):
    j = Journal(tmp_path / "j.db")
    t = lambda d, h: datetime.fromisoformat(f"{d} {h}").replace(tzinfo=IST)
    j.event(t("2026-10-22", "09:05"), "start", "mode=paper replay=False")
    j.trade("paper", "SBIN-EQ", "LONG", 1, t("2026-10-22", "10:00"), 800, t("2026-10-22", "10:05"), 804.8, 4.8, 2.2, "target", "ema_cross")
    j.trade("paper", "SBIN-EQ", "LONG", 1, t("2026-10-21", "10:00"), 800, t("2026-10-21", "10:05"), 797, -3, 2.2, "stop_loss", "ema_cross")
    del j

    class A:
        db, json, since, until, source = str(tmp_path / "j.db"), None, "0000-00-00", "9999-99-99", "all"

    rows = br.load_rows(A)
    assert {r["source"] for r in rows} == {"session", "replay"}
    A.source = "session"
    assert [r["exit_ts"][:10] for r in br.load_rows(A)] == ["2026-10-22"]
