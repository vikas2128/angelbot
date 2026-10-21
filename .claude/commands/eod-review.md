---
description: End-of-day review of today's (or a given date's) trading
argument-hint: [YYYY-MM-DD]
---
Review the trading session for $ARGUMENTS (default: today in IST).

1. `trades(date=...)` → table of trades: time, side, entry, exit, reason, net.
2. `journal_query` on `signals` for that date: how many signals, how many acted, top skip reasons.
3. `journal_query` on `events` for WARNING/ERROR (rejections, feed restarts, stuck exits, halts).
4. Compare to the last 10 sessions (net per day, win rate) using `journal_query`.
5. Summarise: what worked, what didn't, execution quality (slippage vs signal price, chases),
   and at most 3 concrete, testable suggestions. Suggestions must be backtested before any
   config change — offer to run them, don't edit config in this command.
Keep it brief and numeric. Charges count.
