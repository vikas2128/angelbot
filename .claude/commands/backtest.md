---
description: Backtest a strategy with an out-of-sample check
argument-hint: <SYMBOL> [strategy] [params-json]
---
Backtest $ARGUMENTS.

1. `list_data` — if the symbol has no candles, ask me before calling `fetch_candles`.
2. Split available dates ~70/30 into in-sample (older) and out-of-sample (newer).
3. `run_backtest` on both windows (same params). Use config defaults if no strategy/params given.
4. Report a side-by-side table: trades, net P&L, charges, win rate, profit factor, max DD,
   green/red days, exit-reason mix. Flag if out-of-sample degrades by > 50% or trades < 30.
5. One-paragraph verdict, honest about sample size. No config edits.
