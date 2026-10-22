import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot import cli, universe as U

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "config" / "universe"
HEADER = "Company Name,Industry,Symbol,Series,ISIN Code\n"


def csv_text(symbols, series="EQ"):
    return HEADER + "".join(f"Co {s},Fin,{s},{series},INE000{i:05d}\n" for i, s in enumerate(symbols))


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
BANK = [f"BANK{i}" for i in range(12)]


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """An empty shipped dir and an empty refreshed dir, so nothing depends on the real files."""
    shipped, refreshed = tmp_path / "shipped", tmp_path / "refreshed"
    shipped.mkdir()
    monkeypatch.setattr(U, "SHIPPED_DIR", shipped)
    monkeypatch.setattr(U, "refreshed_dir", lambda: refreshed)
    return shipped, refreshed


# ---------------------------------------------------------------- the shipped NSE snapshots
def test_the_shipped_lists_parse_and_are_stocks_only():
    nifty, bank = U.parse((SHIPPED / "nifty50.csv").read_text(), "nifty50"), U.parse((SHIPPED / "banknifty.csv").read_text(), "banknifty")
    assert len(nifty) == 50 and len(bank) == 14 and len(set(nifty)) == 50
    assert {"SBIN", "HDFCBANK", "RELIANCE", "M&M", "BAJAJ-AUTO"} <= set(nifty) and {"YESBANK", "UNIONBANK"} <= set(bank)
    import re
    assert not any(re.search(r"\d(CE|PE)$|FUT$", s) for s in nifty + bank)                        # no option or future contracts
    meta = json.loads((SHIPPED / "meta.json").read_text())
    assert meta["nifty50"]["count"] == 50 and meta["banknifty"]["count"] == 14 and "nseindia.com" in meta["nifty50"]["source"]


def test_expand_the_real_presets_into_one_deduplicated_universe():
    syms, notes = U.expand(["nifty50", "banknifty", "SBIN-EQ"], now=NOW)
    assert len(syms) == 59 and len(set(syms)) == 59 and all(s.endswith("-EQ") for s in syms)
    assert syms.count("SBIN-EQ") == 1                                                             # in both lists and listed by hand: once
    assert {"YESBANK-EQ", "UNIONBANK-EQ", "M&M-EQ"} <= set(syms)
    assert notes == []


# ---------------------------------------------------------------- parsing
def test_parse_checks_the_shape_and_ignores_non_equity():
    assert U.parse(csv_text(BANK), "banknifty") == BANK
    assert U.parse("﻿" + csv_text(BANK), "banknifty") == BANK                                # a BOM is fine
    mixed = csv_text(BANK) + "Odd Co,Fin,ODDBE,BE,INE1\n"
    assert U.parse(mixed, "banknifty") == BANK                                                    # series other than EQ are dropped
    with pytest.raises(U.UniverseError, match="not a constituent list"):
        U.parse("<html>Access denied</html>", "nifty50")
    with pytest.raises(U.UniverseError, match="expected 40 to 60"):
        U.parse(csv_text(["A", "B", "C"]), "nifty50")
    with pytest.raises(U.UniverseError, match="unexpected symbol"):
        U.parse(csv_text(BANK[:11] + ["bad symbol!"]), "banknifty")


# ---------------------------------------------------------------- expand, load and where copies live
def test_expand_names_aliases_and_passthrough(dirs):
    shipped, _ = dirs
    (shipped / "banknifty.csv").write_text(csv_text(BANK))
    syms, notes = U.expand(["SBIN-EQ", "NIFTYBANK", "banknifty", "TCS-EQ"], now=NOW)
    assert syms == ["SBIN-EQ"] + [f"{b}-EQ" for b in BANK] + ["TCS-EQ"] and notes == []
    assert U.canonical(" Nifty_Bank ") == "banknifty" and U.canonical("sensex") is None


def test_a_missing_list_is_a_note_not_a_crash(dirs):
    syms, notes = U.expand(["nifty50", "SBIN-EQ"], now=NOW)
    assert syms == ["SBIN-EQ"] and len(notes) == 1 and "universe refresh" in notes[0]


def test_a_refreshed_copy_wins_over_the_shipped_one(dirs):
    shipped, refreshed = dirs
    (shipped / "banknifty.csv").write_text(csv_text(BANK))
    refreshed.mkdir()
    (refreshed / "banknifty.csv").write_text(csv_text(BANK[:9]))
    syms, info = U.load("banknifty")
    assert len(syms) == 9 and info["where"] == "refreshed"


def test_an_old_list_is_flagged(dirs):
    shipped, _ = dirs
    (shipped / "banknifty.csv").write_text(csv_text(BANK))
    old = (NOW - timedelta(days=U.STALE_DAYS + 5)).isoformat()
    (shipped / "meta.json").write_text(json.dumps({"banknifty": {"fetched_at": old, "source": "x", "count": 12}}))
    syms, notes = U.expand(["banknifty"], now=NOW)
    assert len(syms) == 12 and len(notes) == 1 and f"{U.STALE_DAYS + 5} days old" in notes[0]
    assert U.status(NOW)[1]["stale"] is True


# ---------------------------------------------------------------- refresh
def test_refresh_saves_a_validated_copy_and_reports_what_changed(dirs):
    shipped, refreshed = dirs
    (shipped / "banknifty.csv").write_text(csv_text(BANK))
    newer = BANK[1:] + ["NEWBANK"]
    calls = []

    def fetch(url):
        calls.append(url)
        return csv_text(newer)

    r = U.refresh(["banknifty"], fetch=fetch, now=NOW)[0]
    assert r["ok"] and r["count"] == 12 and r["added"] == ["NEWBANK-EQ"] and r["removed"] == ["BANK0-EQ"]
    assert calls == ["https://nsearchives.nseindia.com/content/indices/ind_niftybanklist.csv"]
    assert (refreshed / "banknifty.csv").read_text() == csv_text(newer)
    meta = json.loads((refreshed / "meta.json").read_text())["banknifty"]
    assert meta["count"] == 12 and meta["fetched_at"].startswith("2026-10-22") and meta["source"] == calls[0]
    assert "NEWBANK-EQ" in U.load("banknifty")[0] and not list(refreshed.glob(".tmp_*"))


def test_refresh_falls_back_to_the_second_host(dirs):
    urls = []

    def fetch(url):
        urls.append(url)
        if "nseindia" in url:
            raise OSError("connection reset")
        return csv_text(BANK)

    r = U.refresh(["banknifty"], fetch=fetch, now=NOW)[0]
    assert r["ok"] and "niftyindices.com" in r["source"] and len(urls) == 2


def test_a_bad_download_never_replaces_a_good_list(dirs):
    shipped, refreshed = dirs
    (shipped / "banknifty.csv").write_text(csv_text(BANK))
    refreshed.mkdir()
    good = csv_text(BANK[:10])
    (refreshed / "banknifty.csv").write_text(good)
    for bad in ("<html>blocked</html>", csv_text(["ONLY", "TWO"]), ""):
        r = U.refresh(["banknifty"], fetch=lambda url, b=bad: b, now=NOW)[0]
        assert not r["ok"] and "nseindia" in r["error"] and "niftyindices" in r["error"]
        assert (refreshed / "banknifty.csv").read_text() == good


def test_refresh_both_lists_by_default(dirs):
    def fetch(url):
        return csv_text([f"S{i}" for i in range(50)]) if "nifty50" in url else csv_text(BANK)
    out = U.refresh(fetch=fetch, now=NOW)
    assert [r["name"] for r in out] == ["nifty50", "banknifty"] and all(r["ok"] for r in out)


# ---------------------------------------------------------------- command and screener
def test_cli_show_and_refresh(dirs, capsys, monkeypatch):
    shipped, _ = dirs
    (shipped / "banknifty.csv").write_text(csv_text(BANK))
    assert cli.cmd_universe(argparse.Namespace(action="show", name=None), {}) is None
    out = capsys.readouterr().out
    assert "Bank Nifty" in out and "12 stocks" in out and "list not found" in out                  # nifty50 has no copy here
    monkeypatch.setattr(U, "refresh", lambda names: [{"name": "banknifty", "title": "Bank Nifty", "ok": False, "error": "offline"}])
    assert cli.cmd_universe(argparse.Namespace(action="refresh", name="banknifty"), {}) == 1
    assert "NOT updated: offline" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="unknown list"):
        cli.cmd_universe(argparse.Namespace(action="show", name="sensex"), {})


def test_the_screener_accepts_presets(dirs, monkeypatch):
    from bot import screen_data
    from test_screener import QUOTE, FakeBroker, NOW as SNOW
    monkeypatch.setattr(U, "load", lambda name: (["AAA-EQ", "ZZZ-EQ"], {"title": "Nifty 50", "fetched_at": None}))
    master = [{"symbol": "AAA-EQ", "token": "11", "exch_seg": "NSE", "lotsize": "1", "tick_size": "5"},
              {"symbol": "BBB-EQ", "token": "22", "exch_seg": "NSE", "lotsize": "1", "tick_size": "5"}]
    snap = screen_data.run_screen({}, FakeBroker(QUOTE), master, SNOW, universe=["nifty50", "BBB-EQ"], pause=0)
    assert snap["universe"] == 2                                                                   # AAA and BBB resolved
    assert any("ZZZ-EQ: not found in the instrument master" in e for e in snap["errors"])


def test_membership_tags_each_stock_with_its_index(dirs):
    shipped, _ = dirs
    (shipped / "nifty50.csv").write_text(csv_text([f"S{i}" for i in range(48)] + ["BANK0", "BANK1"]))
    (shipped / "banknifty.csv").write_text(csv_text(BANK))
    lists, member = U.membership(["nifty50", "banknifty", "XYZ-EQ", "niftybank"], now=NOW)
    assert [(x["title"], x["tag"], x["count"]) for x in lists] == [("Nifty 50", "N50", 50), ("Bank Nifty", "BANK", 12)]
    assert member["BANK0-EQ"] == ["N50", "BANK"] and member["S3-EQ"] == ["N50"] and member["BANK5-EQ"] == ["BANK"]
    assert "XYZ-EQ" not in member


def test_the_screen_snapshot_carries_lists_and_tags(dirs, monkeypatch):
    from bot import screen_data
    from test_screener import QUOTE, FakeBroker, NOW as SNOW
    shipped, _ = dirs
    (shipped / "banknifty.csv").write_text(csv_text(["AAA"] + BANK[:11]))
    master = [{"symbol": "AAA-EQ", "token": "11", "exch_seg": "NSE", "lotsize": "1", "tick_size": "5"},
              {"symbol": "BBB-EQ", "token": "22", "exch_seg": "NSE", "lotsize": "1", "tick_size": "5"}]
    snap = screen_data.run_screen({}, FakeBroker(QUOTE), master, SNOW, universe=["banknifty", "BBB-EQ"], pause=0)
    tags = {r["symbol"]: r["indices"] for r in snap["rows"]}
    assert tags == {"AAA-EQ": ["BANK"], "BBB-EQ": []} and [x["tag"] for x in snap["lists"]] == ["BANK"]
