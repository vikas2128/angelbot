from datetime import datetime
from pathlib import Path

from bot.timeutil import BOT_START, IST, market_clock

SESSION = {"shutdown": "15:32", "holidays": ["2026-10-26"]}


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=IST)


def test_friday_after_close_skips_the_weekend_and_a_monday_holiday():
    m = market_clock(at("2026-10-23 15:40"), SESSION)
    assert m["trading_day"] and not m["open"] and m["session_over"]
    assert m["next_start"].startswith("2026-10-27T09:05")


def test_open_hours_and_before_the_start():
    m = market_clock(at("2026-10-22 11:00"), SESSION)
    assert m["open"] and not m["session_over"] and m["next_start"].startswith("2026-10-23T09:05")
    m = market_clock(at("2026-10-22 08:00"), SESSION)
    assert not m["open"] and not m["session_over"] and m["next_start"].startswith("2026-10-22T09:05")


def test_saturday_and_holiday_are_not_trading_days():
    for t in ("2026-10-24 11:00", "2026-10-26 11:00"):
        m = market_clock(at(t), SESSION)
        assert not m["trading_day"] and not m["open"] and m["session_over"]


def test_bot_start_matches_the_systemd_timer():
    timer = (Path(__file__).parent.parent / "deploy" / "angelbot@.timer").read_text()
    assert f"{BOT_START}:00" in timer
