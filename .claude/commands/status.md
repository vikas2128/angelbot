---
description: Quick health check of the trading bot
---
Call `bot_status` and `service_status`. Report in ≤10 lines:
- alive? mode (paper/live)? state, strategy, instruments
- day P&L (realised / unrealised / charges), trades today, open positions with stop/target
- pending orders, halts/cooldowns/pause/kill flags, feed health (last tick age, restarts)
If anything looks wrong (stale heartbeat in market hours, stuck exit, rejected orders, feed
restarts > 3), pull `tail_log(contains="ERROR")` and explain the likely cause. Do not act
(pause/kill) unless I ask.
