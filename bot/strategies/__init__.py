"""Strategy registry. To add one: subclass Strategy, then register it here."""
from bot.strategies.base import Strategy
from bot.strategies.ema_cross import EmaCross
from bot.strategies.orb import OpeningRangeBreakout
from bot.strategies.vwap_revert import VwapReversion

STRATEGIES: dict[str, type[Strategy]] = {
    EmaCross.name: EmaCross,
    OpeningRangeBreakout.name: OpeningRangeBreakout,
    VwapReversion.name: VwapReversion,
}


def build(name: str, params: dict | None = None) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; options: {list(STRATEGIES)}")
    return STRATEGIES[name](params or {})
