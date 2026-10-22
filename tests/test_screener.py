from datetime import datetime

import pytest

from bot.screener import DEFAULTS, gates, metrics, params_from, percentiles, rank
from bot.timeutil import IST

CHARGES = {"brokerage_flat": 20, "brokerage_pct": 0.1, "stt_sell_pct": 0.025, "exchange_txn_pct": 0.00297,
           "sebi_per_crore": 10, "stamp_buy_pct": 0.003, "gst_pct": 18}
NOW = datetime(2026, 10, 22, 9, 17, tzinfo=IST)


def daily(n=20, close=1000.0, vol=2_000_000, rng_pct=1.5):
    out = []
    for i in range(n):
        c = close * (1 + 0.001 * (i % 3))
        out.append({"d": f"2026-09-{i + 1:02d}", "o": c, "h": c * (1 + rng_pct / 200), "l": c * (1 - rng_pct / 200), "c": c, "v": vol})
    return out


def raw(symbol, **kw):
    r = {"symbol": symbol, "token": "1", "ltp": 1000.0, "open": 1000.0, "prev_close": 1000.0, "volume": 100_000,
         "bid": 999.95, "ask": 1000.05, "daily": daily()}
    r.update(kw)
    return r


def test_percentiles_ties_and_single():
    assert percentiles({"a": 5}) == {"a": 1.0}
    p = percentiles({"a": 1, "b": 2, "c": 2, "d": 3})
    assert p["a"] == 0.0 and p["d"] == 1.0 and p["b"] == p["c"] == pytest.approx(0.5)


def test_metrics_are_worked_out_from_the_raw_data():
    m = metrics(raw("X", open=1020.0, prev_close=1000.0, volume=400_000), DEFAULTS, CHARGES)
    assert m["gap_pct"] == 2.0                                   # (1020-1000)/1000
    assert m["spread_pct"] == pytest.approx(0.01, abs=0.001)     # 0.10 on 1000
    assert m["vol_pct_of_avg"] == 20.0                           # 400k of a 2M day
    assert m["traded_value_cr"] == pytest.approx(200.0, abs=1.0)  # ~1000 x 2M shares = 2e9 rupees = 200 crore
    assert m["suggested_qty"] == 40 and m["round_trip_cost_pct"] > 0
    assert m["range_to_cost"] == pytest.approx(m["range_pct"] / m["round_trip_cost_pct"], rel=0.01)


def test_missing_inputs_are_none_not_guesses():
    m = metrics(raw("X", open=0, bid=0, ask=0, volume=None), DEFAULTS, CHARGES)
    assert m["gap_pct"] is None and m["spread_pct"] is None and m["vol_pct_of_avg"] is None


def test_gates_explain_themselves():
    thin = metrics(raw("THIN", daily=daily(vol=1000)), DEFAULTS, CHARGES)
    assert any("traded value" in g for g in gates(thin, DEFAULTS))
    wide = metrics(raw("WIDE", bid=990.0, ask=1010.0), DEFAULTS, CHARGES)
    assert any("spread" in g for g in gates(wide, DEFAULTS))
    flat = metrics(raw("FLAT", daily=daily(rng_pct=0.2)), DEFAULTS, CHARGES)
    assert any("x costs" in g for g in gates(flat, DEFAULTS))
    new = metrics(raw("NEW", daily=daily(n=4)), DEFAULTS, CHARGES)
    assert gates(new, DEFAULTS) == ["only 4 days of history (need 10)"]


def test_rank_orders_by_the_factors_and_lists_the_excluded():
    rows = [raw("QUIET", open=1000.0, volume=50_000, daily=daily(rng_pct=1.2)),
            raw("HOT", open=1025.0, prev_close=1000.0, volume=900_000, daily=daily(rng_pct=2.5, vol=3_000_000)),
            raw("MID", open=1008.0, volume=300_000, daily=daily(rng_pct=1.8)),
            raw("THIN", daily=daily(vol=500))]
    snap = rank(rows, params_from(None), CHARGES, NOW)
    assert [r["symbol"] for r in snap["rows"][:3]] == ["HOT", "MID", "QUIET"]
    assert snap["rows"][0]["rank"] == 1 and snap["rows"][0]["score"] > snap["rows"][2]["score"]
    assert snap["picks"] == ["HOT", "MID", "QUIET"] and snap["eligible"] == 3 and snap["universe"] == 4
    thin = snap["rows"][-1]
    assert thin["symbol"] == "THIN" and thin["rank"] is None and thin["excluded"]
    assert snap["market_date"] == "2026-10-22" and all(r["why"] for r in snap["rows"][:3])


def test_a_missing_factor_does_not_sink_a_stock():
    rows = [raw("A", open=0, volume=None), raw("B", open=1010.0, volume=200_000)]
    snap = rank(rows, params_from(None), CHARGES, NOW)
    a = next(r for r in snap["rows"] if r["symbol"] == "A")
    assert a["score"] is not None and "no gap data yet" in a["why"]


def test_params_from_config_overrides_defaults_only_for_known_keys():
    p = params_from({"screener": {"top_n": 5, "order_value": 25000, "weights": {"gap": 0.5}, "bogus": 1}})
    assert p["top_n"] == 5 and p["order_value"] == 25000 and p["weights"]["gap"] == 0.5
    assert p["weights"]["volume"] == DEFAULTS["weights"]["volume"] and "bogus" not in p
    assert DEFAULTS["top_n"] == 3 and DEFAULTS["weights"]["gap"] == 0.25      # defaults untouched


# ---------------------------------------------------------------- data collection and report
from bot.models import Instrument
from bot.screen_data import collect
from bot.screen_report import render_html, summary_html


class FakeBroker:
    def __init__(self, quotes=None, fail=(), quotes_fail=False):
        self._quotes, self.fail, self.quotes_fail, self.asked = quotes or {}, set(fail), quotes_fail, []

    def quotes(self, by_exchange):
        self.asked.append(by_exchange)
        if self.quotes_fail:
            raise RuntimeError("boom")
        return self._quotes

    def candles(self, inst, interval, start, end):
        assert interval == "ONE_DAY"
        if inst.symbol in self.fail:
            raise RuntimeError("no data")
        rows = [[f"2026-10-{d:02d}T00:00:00+0530", 100, 102, 99, 101, 5000] for d in range(1, 22)]
        rows.append(["2026-10-22T00:00:00+0530", 101, 103, 100, 102, 100])      # today's forming candle
        return rows


INSTS = [Instrument("AAA-EQ", "11", "NSE"), Instrument("BBB-EQ", "22", "NSE")]
QUOTE = {"11": {"symbolToken": "11", "ltp": 102.5, "open": 102.0, "close": 100.0, "tradeVolume": 1500,
                "depth": {"buy": [{"price": 102.45}], "sell": [{"price": 102.55}]}}}


def test_collect_maps_quotes_drops_todays_candle_and_reports_gaps():
    raws, errs = collect(FakeBroker(QUOTE), INSTS, NOW, pause=0)
    a = raws[0]
    assert (a["ltp"], a["open"], a["prev_close"], a["volume"], a["bid"], a["ask"]) == (102.5, 102.0, 100.0, 1500, 102.45, 102.55)
    assert a["daily"][-1]["d"] == "2026-10-21" and len(a["daily"]) == 21          # 22 Oct is not included
    assert raws[1]["ltp"] is None and "BBB-EQ: no quote returned" in errs        # no quote: left empty, said so


def test_collect_survives_failures():
    raws, errs = collect(FakeBroker(quotes_fail=True), INSTS, NOW, pause=0)       # quotes call raises
    assert any("quotes failed" in e for e in errs) and len(raws) == 2 and raws[0]["ltp"] is None
    raws, errs = collect(FakeBroker(QUOTE, fail=["BBB-EQ"]), INSTS, NOW, pause=0)
    assert [r["symbol"] for r in raws] == ["AAA-EQ"] and any("BBB-EQ: no daily candles" in e for e in errs)


def _snap():
    rows = [raw("HOT", open=1025.0, volume=900_000, daily=daily(rng_pct=2.5, vol=3_000_000)), raw("MID", open=1008.0, volume=300_000),
            raw("THIN", daily=daily(vol=500))]
    return rank(rows, params_from(None), CHARGES, NOW, ["XYZ-EQ: not found in the instrument master"])


def test_report_html_shows_everything_without_javascript():
    page = render_html(_snap())
    assert "<script" not in page and "http://" not in page and "https://" not in page
    import re
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", page, flags=re.S)))
    for needle in ("Stock screener", "Thu 22 Oct", "Advisory only", "HOT", "MID", "+2.5%", "Left out", "THIN",
                   "traded value", "Problems collecting data", "XYZ-EQ", "How the score works", "has not been backtested"):
        assert needle in text, needle
    assert text.index("HOT") < text.index("MID")                                  # ranked order


def test_report_html_escapes_and_handles_no_eligible_stocks():
    snap = rank([raw("<b>X", daily=daily(vol=500))], params_from(None), CHARGES, NOW)
    page = render_html(snap)
    assert "&lt;b&gt;X" in page and "<b>X" not in page and "No stock passed the gates today" in page


def test_summary_message_flags_a_stale_snapshot():
    snap = _snap()
    fresh = summary_html(snap, "2026-10-22")
    assert "HOT" in fresh and "Advisory only" in fresh and "not today" not in fresh
    assert "from 2026-10-22, not today" in summary_html(snap, "2026-10-23")


def test_collect_is_paced_and_reports_progress(monkeypatch):
    sleeps, seen = [], []
    monkeypatch.setattr("bot.screen_data.time.sleep", lambda s: sleeps.append(s))
    collect(FakeBroker(QUOTE), INSTS, NOW, pause=1.0, progress=lambda i, n, sym: seen.append((i, n, sym)))
    assert sleeps == [1.0, 1.0, 1.0, 1.0]            # after login, after quotes, after each of the 2 stocks
    assert seen == [(1, 2, "AAA-EQ"), (2, 2, "BBB-EQ")]


# ---------------------------------------------------------------- lists, index tags and tabs
def _tagged_snap(n_bad=7):
    rows = [raw("SBIN-EQ", open=1020.0, volume=900_000), raw("TCS-EQ", open=1005.0, volume=300_000),
            raw("YESBANK-EQ", open=990.0, volume=500_000)] + [raw(f"THIN{i}-EQ", daily=daily(vol=500)) for i in range(n_bad)]
    snap = rank(rows, params_from(None), CHARGES, NOW)
    tags = {"SBIN-EQ": ["N50", "BANK"], "TCS-EQ": ["N50"], "YESBANK-EQ": ["BANK"], "THIN0-EQ": ["N50"]}
    for r in snap["rows"]:
        r["indices"] = tags.get(r["symbol"], [])
    snap["lists"] = [{"name": "nifty50", "title": "Nifty 50", "tag": "N50", "count": 50, "fetched_at": "2026-10-22T13:47:00+05:30", "stale": False},
                     {"name": "banknifty", "title": "Bank Nifty", "tag": "BANK", "count": 14, "fetched_at": "2026-10-22T13:47:00+05:30", "stale": False}]
    return snap


def _text(page):
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", page, flags=re.S)))


def test_page_says_which_lists_were_screened_and_tags_each_stock():
    page = render_html(_tagged_snap())
    t = _text(page)
    assert "Screened: Nifty 50 (50, list from 22 Oct) + Bank Nifty (14, list from 22 Oct)" in t
    assert '<td>SBIN<span class="tag">N50</span><span class="tag">BANK</span>' in page               # -EQ dropped on the page
    assert '<td>YESBANK<span class="tag">BANK</span>' in page and '<b>THIN0</b><span class="tag">N50</span>' in page


def test_page_has_tabs_per_list_and_a_left_out_tab_without_javascript():
    page = render_html(_tagged_snap())
    assert "<script" not in page
    for k, label in (("all", "All (3)"), ("n50", "Nifty 50 (2)"), ("bank", "Bank Nifty (2)"), ("out", "Left out (7)")):
        assert f'<label for="st-{k}" role="tab">{label}</label>' in page, label
        assert f'id="sp-{k}"' in page and f"#st-{k}:checked~#sp-{k}{{display:block}}" in page
    assert '<input class="tab-in" type="radio" name="st" id="st-all" checked>' in page and page.count(" checked>") == 1
    n50 = page.split('id="sp-n50"', 1)[1].split('id="sp-bank"', 1)[0]
    assert "<td>SBIN<" in n50 and "<td>TCS<" in n50 and "YESBANK" not in n50                 # each tab shows only its index
    out = page.split('id="sp-out"', 1)[1]
    assert out.count("<li><b>THIN") == 7                                                      # the left-out list sits in its own tab


def test_a_hand_picked_list_gets_plain_tabs():
    snap = rank([raw("A-EQ"), raw("B-EQ", daily=daily(vol=500))], params_from(None), CHARGES, NOW)
    page = render_html(snap)
    assert "Screened: a hand-picked list of stocks" in _text(page)
    assert '<label for="st-all"' in page and '<label for="st-out"' in page and 'for="st-n50"' not in page


def test_telegram_summary_names_the_lists_and_tags_the_picks():
    text = summary_html(_tagged_snap(), "2026-10-22")
    assert "Screened: Nifty 50 (50, list from 22 Oct) + Bank Nifty (14, list from 22 Oct)" in text
    assert "<b>SBIN-EQ</b> [N50] [BANK]" in text and "<b>YESBANK-EQ</b> [BANK]" in text
    snap = _tagged_snap()
    snap["lists"][1]["stale"], snap["lists"][1]["age_days"] = True, 130
    assert "Bank Nifty (14, list from 22 Oct, 130 days old: refresh it)" in summary_html(snap, "2026-10-22")
