---
description: Investigate a problem (errors, rejected orders, stuck exits, feed drops)
argument-hint: [what you noticed]
---
Investigate: $ARGUMENTS

1. `bot_status` first. If there is an OPEN POSITION with a stuck/failed exit during market hours,
   say so at the top and recommend I check the Angel One app immediately; offer `kill_switch`
   (needs my explicit OK) — do not call it yourself.
2. `tail_log(contains="ERROR")`, `tail_log(contains="WARNING")`, events table for today.
3. `journalctl --user -u angelbot -n 300 --no-pager` for crashes/restarts.
4. Identify root cause (auth/session expiry, static IP mismatch, rejection text, rate limit,
   feed stale, code exception). Quote the exact log lines.
5. Propose a fix. Code fixes: write the patch + a regression test, run pytest, and tell me it
   needs a service restart (which I approve).
