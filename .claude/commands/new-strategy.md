---
description: Implement, test and backtest a new strategy idea
argument-hint: <idea in plain words>
---
Implement this strategy idea: $ARGUMENTS

1. Restate the rules precisely (entry long/short, exit, filters, params with defaults). Ask me
   if anything is ambiguous before coding.
2. Create `bot/strategies/<name>.py` subclassing `Strategy` (see ema_cross.py for the pattern):
   pure function of today's closed bars + current side; no I/O, no broker calls, no lookahead.
3. Register it in `bot/strategies/__init__.py`; add `strategy.params.<name>` to config.yaml
   WITHOUT switching `strategy.name`.
4. Add `tests/test_strategy_<name>.py` with hand-built bars covering long, short/flat and
   no-signal cases. Run `./.venv/bin/python -m pytest -q`.
5. Run `/backtest`-style in-sample vs out-of-sample comparison against the current strategy.
6. Report results honestly. Switching the active strategy is my decision.
