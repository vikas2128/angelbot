import copy

from bot import settings as ST
from bot import settings_guide as SG


def base(cfg, **over):
    c = copy.deepcopy(cfg)
    c.setdefault("selection", {})["mode"] = "auto"
    c["sizing"] = {"amount": 10000}
    c["risk"]["max_open_positions"] = 5
    c["risk"]["max_daily_loss"] = 500
    c["exits"].update(stop_loss_pct=0.3, target_pct=0.6)
    c.setdefault("screener", {})["strategy_per_stock"] = True
    for k, v in over.items():
        sec, key = k.split("__")
        c.setdefault(sec, {})[key] = v
    return c


def test_every_setting_has_a_section_and_help():
    assert {s["id"] for s in ST.SETTINGS} == set(SG.SECTION_OF) == set(SG.HELP)


def test_only_what_applies_is_shown(cfg):
    x = SG.explain(base(cfg))
    assert x["selqty"]["hidden"] and x["listqty"]["hidden"] and "amount per trade" in x["selqty"]["note"]
    assert x["btgate"]["hidden"] and not x["perstock"]["hidden"] and not x["topn"]["hidden"]
    off = SG.explain(base(cfg, sizing__amount=0, selection__mode="off", screener__strategy_per_stock=False))
    assert not off["listqty"]["hidden"] and off["topn"]["hidden"] and off["perstock"]["hidden"] and not off["btgate"]["hidden"]


def test_rupee_effects_match_the_fee_model(cfg):
    c = base(cfg)
    fees = SG._round_trip(c, 10000)
    x = SG.explain(c)
    assert "Up to ₹50,000 in use with 5 positions" in x["amount"]["impact"]
    assert f"₹30 + ₹{fees:,.0f} fees" in x["sl"]["impact"] and f"= ₹{60 - fees:,.0f}" in x["tgt"]["impact"]
    assert f"About {int(500 // (30 + fees))} losing trades" in x["maxloss"]["impact"]
    assert SG.explain(base(cfg, sizing__amount=0))["sl"]["impact"] == ""        # no amount: no rupee guess


def test_short_values_and_payload(cfg):
    assert SG.short(ST.BY_ID["selmode"], "confirm") == "Ask me" and SG.short(ST.BY_ID["perstock"], True) == "On"
    assert SG.short(ST.BY_ID["univ"], ["nifty50", "banknifty"]) == "Nifty 50 + Bank Nifty"
    assert SG.short(ST.BY_ID["amount"], 0) == "Off" and SG.short(ST.BY_ID["topn"], 1) == "1 stock"
    d = SG.payload(base(cfg), {"risk.max_daily_loss": 500}, [], "paper")
    assert [s["key"] for s in d["sections"]] == ["size", "stocks", "risk", "exits"] and d["changed"] == 1
    item = next(i for s in d["sections"] for i in s["items"] if i["id"] == "maxloss")
    assert item["changed"] and item["help"] and any(c["current"] for c in item["choices"])
