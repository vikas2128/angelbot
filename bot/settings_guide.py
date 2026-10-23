"""Plain-language guide for the app's Settings screen: sections, what each setting does, friendly choice names,
which settings apply right now, and what a choice means in rupees. Display only: values and validation stay in
bot/settings.py, so the app and Telegram can never disagree about what is saved."""
from __future__ import annotations

from bot import settings as ST
from bot.charges import order_charges

SECTIONS = [
    ("size", "Money per trade", "How big each trade is"),
    ("stocks", "Which stocks to trade", "How the bot picks today's stocks and how it trades them"),
    ("risk", "Safety limits", "Shared by all stocks, for the whole day"),
    ("exits", "Exits on every trade", "Where each trade takes its loss or its profit"),
]
SECTION_OF = {"amount": "size", "selqty": "size", "listqty": "size",
              "selmode": "stocks", "topn": "stocks", "univ": "stocks", "perstock": "stocks", "btgate": "stocks", "strat": "stocks",
              "maxpos": "risk", "maxloss": "risk", "maxtr": "risk",
              "sl": "exits", "tgt": "exits"}
HELP = {
    "amount": "Rupees for each trade. The bot buys as many whole shares as fit, so an order never costs more. "
              "Off uses fixed share counts instead.",
    "selqty": "Fixed shares for each stock the morning screen picks (only when the amount per trade is off).",
    "selmode": "Auto: at 09:16 the bot screens and trades the top picks. Ask me: it sends the picks to Telegram and "
               "waits for your tap until 09:19. Off: it trades only the stocks in your config list.",
    "listqty": "Fixed shares for the stocks in your config list (only when the amount per trade is off).",
    "topn": "How many of the screen's top stocks to trade each day.",
    "univ": "The pool the morning screen ranks. More stocks means more chances that some pass the checks; "
            "the screen takes about a second per stock.",
    "perstock": "On: each night every strategy is tested on each stock. A stock trades the strategy that made money "
                "on the older days and again on the newer days; a stock where none does is skipped.",
    "btgate": "Skip stocks whose recent backtest with your strategy lost money after fees.",
    "strat": "The strategy for your config list, and for every stock when Strategy per stock is off.",
    "maxpos": "How many stocks can be held at the same time.",
    "maxloss": "If today's result after fees falls this far, the bot closes everything and stops for the day.",
    "maxtr": "Most trades in a day, across all stocks together. Limits fees on choppy days.",
    "sl": "Each trade exits if the price moves this far against you.",
    "tgt": "Each trade takes its profit when the price moves this far in your favour.",
}
CHOICE_NAMES = {
    "selmode": {"off": "Off", "auto": "Auto", "confirm": "Ask me"},
    "strat": {"ema_cross": "EMA crossover", "orb": "Opening range", "vwap_revert": "VWAP reversion"},
    "univ": {"builtin": "Built-in 20", "nifty50": "Nifty 50", "banknifty": "Bank Nifty", "nifty50+banknifty": "Nifty 50 + Bank Nifty"},
}


def short(s: dict, v) -> str:
    """A value as a few words (the row shows this; the full sentence is in the help)."""
    sid, p = s["id"], s["path"]
    if v is None:
        return "–"
    if isinstance(v, bool) or p in ("screener.strategy_per_stock", "screener.backtest_gate"):
        return "On" if v else "Off"
    if sid == "univ":
        return CHOICE_NAMES["univ"].get(v if isinstance(v, str) else "+".join(v), ST.show(s, v))
    if sid in CHOICE_NAMES:
        return CHOICE_NAMES[sid].get(v, str(v))
    if sid == "amount":
        return f"₹{float(v):,.0f}" if float(v) else "Off"
    if sid == "maxloss":
        return f"₹{float(v):,.0f}"
    if p.startswith("exits."):
        return f"{float(v):g}%"
    if p.endswith(".qty"):
        return f"{int(v)} share{'s' if int(v) != 1 else ''}"
    if sid in ("topn", "maxpos"):
        return f"{int(v)} stock{'s' if int(v) != 1 else ''}"
    if sid == "maxtr":
        return f"{int(v)} trades"
    return str(v)


def _round_trip(cfg: dict, amount: float) -> float:
    c = cfg.get("charges") or {}
    return order_charges("BUY", 1, amount, c) + order_charges("SELL", 1, amount, c)


def explain(cfg: dict) -> dict:
    """{setting id: {"hidden": bool, "note": str, "impact": str}} for today's merged config."""
    amount = float((cfg.get("sizing") or {}).get("amount") or 0)
    positions = int((cfg.get("risk") or {}).get("max_open_positions") or 1)
    sl = float((cfg.get("exits") or {}).get("stop_loss_pct") or 0)
    tgt = float((cfg.get("exits") or {}).get("target_pct") or 0)
    loss_limit = float((cfg.get("risk") or {}).get("max_daily_loss") or 0)
    sel_on = str((cfg.get("selection") or {}).get("mode", "off")) != "off"
    per_stock = bool((cfg.get("screener") or {}).get("strategy_per_stock"))
    fees = _round_trip(cfg, amount) if amount else 0.0
    out: dict[str, dict] = {k: {"hidden": False, "note": "", "impact": ""} for k in SECTION_OF}
    if amount:
        for k in ("selqty", "listqty"):
            out[k].update(hidden=True, note="Not used: shares are worked out from the amount per trade.")
        out["amount"]["impact"] = (f"Up to ₹{amount * positions:,.0f} in use with {positions} position{'s' if positions != 1 else ''}; "
                                   f"fees about ₹{fees:,.0f} a round trip ({fees / amount * 100:.2f}%).")
        out["maxpos"]["impact"] = f"Up to ₹{amount * positions:,.0f} in use at ₹{amount:,.0f} each."
        stop_cost = amount * sl / 100 + fees
        out["sl"]["impact"] = f"A stop costs about ₹{amount * sl / 100:,.0f} + ₹{fees:,.0f} fees on a ₹{amount:,.0f} trade."
        out["tgt"]["impact"] = (f"A target gains about ₹{amount * tgt / 100:,.0f} − ₹{fees:,.0f} fees = "
                                f"₹{amount * tgt / 100 - fees:,.0f} on a ₹{amount:,.0f} trade.")
        if stop_cost > 0 and loss_limit:
            out["maxloss"]["impact"] = f"About {int(loss_limit // stop_cost)} losing trades (₹{stop_cost:,.0f} each) before the bot stops."
    elif sel_on:
        out["listqty"]["note"] = "Used only if the morning screen falls back to your config list."
    if not sel_on:
        out["topn"].update(hidden=True, note="Not used while stock selection is off.")
        out["perstock"].update(hidden=True, note="Not used while stock selection is off.")
        out["selqty"].update(hidden=True, note="Not used while stock selection is off.")
    if per_stock:
        out["btgate"].update(hidden=True, note="Included in Strategy per stock.")
        out["strat"]["note"] = "Picked stocks use their own checked strategy; this one is for your config list."
    out["maxtr"]["impact"] = "Shared by all stocks." if positions > 1 else ""
    return out


def payload(cfg: dict, overrides: dict, problems: list[str], area: str | None) -> dict:
    """Everything the app's Settings screen shows, grouped by section."""
    info = explain(cfg)
    sections = []
    for key, title, sub in SECTIONS:
        items = []
        for s in ST.SETTINGS:
            if SECTION_OF.get(s["id"]) != key:
                continue
            cur, x = ST.current(cfg, s), info[s["id"]]
            items.append({"id": s["id"], "label": s["label"], "help": HELP.get(s["id"], ""), "value": short(s, cur),
                          "changed": s["path"] in overrides, "typed": bool(s.get("typed")), **x,
                          "choices": [{"index": i, "label": short(s, c), "current": c == cur} for i, c in enumerate(s["choices"])]})
        sections.append({"key": key, "title": title, "sub": sub, "items": items})
    return {"area": area, "sections": sections, "problems": problems,
            "changed": len([k for k in overrides]), "applies": "Changes apply from the bot's next start (or a restart)."}
