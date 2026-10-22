# AngelBot — guide for Claude Code

Intraday trading bot for **Angel One SmartAPI** (NSE/NFO), running on an OCI VM.
Coded strategy + risk rules make every trading decision in real time. **You (Claude) are
the analyst and operator, not the trader**: monitor, review, backtest, tune, and write
code — never place orders yourself.

## Hard rules
1. **Never place, modify or cancel individual orders** — there is deliberately no tool for it.
   Don't write ad-hoc scripts that call SmartAPI order endpoints either.
2. **Never read, print or edit `.env`** (credentials, TOTP secret, trading mode). Never try to
   switch paper → live. Going live is a manual, human-only step (see README "Going live").
3. `kill_switch` only when the user explicitly asks. `pause_bot` is the gentle option.
4. Every config change: edit `config/config.yaml` → run `validate_config` → backtest it →
   tell the user it applies from the next session (or after a service restart they approve).
5. Don't tune on the same data you report results on. Use walk-forward: optimise on an
   older window, then confirm on a later unseen window. Always report **net P&L after
   charges, trade count, win rate, profit factor and max drawdown** — never just "profit".
6. Keep `risk.max_orders_per_sec` < 10 (SEBI algo threshold) and orders LIMIT-only
   (MARKET/IOC are not allowed for algo orders). Every trade must have a stop loss.
7. Be honest: small samples (< 100 trades) and synthetic data prove nothing.

## Layout
```
bot/engine.py        event loop: ticks → bars → strategy → risk → orders; stops, limits, control
bot/strategies/      ema_cross, orb, vwap_revert  (add new ones + register in __init__.py)
bot/risk.py          entry gates, daily loss/profit halts, cooldown, order throttle
bot/screener.py      advisory stock ranking (pure logic); screen_data.py / screen_report.py: data + HTML (never trades)
bot/selection.py     opt-in morning pick of today's instruments from the screener (selection.mode in config; default off)
bot/orders.py        LIMIT orders, fill tracking, exit chasing, positions, P&L
bot/broker.py        SmartAPI live adapter       bot/paper.py   paper broker (same interface)
bot/feed.py          WebSocket V2 feed + replay   bot/backtest.py bar-based backtester
bot/journal.py       SQLite: trades, orders, signals, events (runtime/journal.db)
bot/control.py       runtime/control.json (pause/kill) + runtime/status.json (heartbeat)
mcp_server/server.py MCP tools you use (angelbot server, see .mcp.json)
config/config.yaml   strategy, instruments, risk, session, execution — you MAY edit (with approval)
```

## Common commands
```bash
./.venv/bin/python -m bot.cli status
./.venv/bin/python -m bot.cli backtest SBIN-EQ --from 2026-08-01 --to 2026-09-15 --strategy orb
./.venv/bin/python -m bot.cli run --replay data/candles/SBIN-EQ_ONE_MINUTE.csv   # offline paper replay
./.venv/bin/python -m pytest -q
journalctl --user -u angelbot -n 200 --no-pager
systemctl --user status angelbot
```

## Writing a new strategy
Subclass `bot.strategies.base.Strategy`, implement `on_bar(bars, side) -> +1 | -1 | 0 | None`
(desired position; None = no opinion), set `name` and `defaults`, register it in
`bot/strategies/__init__.py`, add params under `strategy.params.<name>` in config, add a unit
test in `tests/`, then backtest + walk-forward before suggesting it.

## Slash commands
/status · /eod-review · /report · /backtest · /tune · /new-strategy · /preflight · /incident
