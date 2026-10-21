---
description: Go-live readiness checklist (read-only)
---
Run a go-live readiness review. Do not change anything.

Check and report PASS/FAIL/UNKNOWN for each:
1. `validate_config` passes; stop_loss_pct > 0; max_daily_loss is small relative to capital;
   max_orders_per_sec < 10; qty is minimal (1 share / 1 lot).
2. Paper track record: `journal_query` → ≥ 15 paper sessions, ≥ 50 paper trades, net after
   charges, max daily loss never breached unexpectedly, no unexplained ERROR events in last 5 days.
3. Backtest vs paper consistency for the same period (similar trade count & P&L sign).
4. Execution: share of entries cancelled on timeout, average exit chases, any stuck exits.
5. `service_status`: timer enabled, next run time correct (IST, weekday).
6. Holidays list in config covers the next 3 months.
7. Manual items I must confirm (list them, you can't verify): static IP registered in SmartAPI
   and matches `python -m bot.cli check`; Telegram alerts working; I know how to flatten from
   the Angel One app; capital I can afford to lose.
End with: "Going live is a manual step: edit .env yourself (see README)". Never edit .env.
