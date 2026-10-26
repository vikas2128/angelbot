from __future__ import annotations

from typing import Optional

from bot.models import Bar


class Strategy:
    """One instance per instrument.

    on_bar() receives all of TODAY's closed bars (oldest→newest) plus the current
    position side (+1 long, -1 short, 0 flat) and returns the DESIRED side:
        +1 → be long, -1 → be short, 0 → be flat, None → no opinion (keep as is).
    The engine applies allow_short, risk checks, stops and targets on top.
    """

    name = "base"
    defaults: dict = {}
    warmup_bars = 0   # bars of history wanted before signals are meaningful
    # walk-forward check (bot.stock_backtest.choose): trades needed on the older days to choose on, and on the newer
    # days to confirm. A strategy that can only trade about once a day sets lower numbers (weaker evidence).
    wf_min_trades = (12, 5)

    def __init__(self, params: Optional[dict] = None):
        self.p = {**self.defaults, **(params or {})}

    def reset_day(self) -> None:
        """Called at the start of each session."""

    def on_bar(self, bars: list[Bar], side: int) -> Optional[int]:
        raise NotImplementedError

    def state(self, bars: list[Bar], ltp: float | None = None) -> dict:
        """What the strategy currently sees, for status displays only (never used for decisions).
        Subclasses add their own numbers; `bars` and `needed` tell whether it has warmed up."""
        return {"bars": len(bars), "needed": int(self.warmup_bars)}

    def describe(self) -> str:
        return f"{self.name}({', '.join(f'{k}={v}' for k, v in self.p.items())})"
