---
name: angelbot-report
description: Build a standalone HTML performance report (KPIs, running P&L, daily results, exit reasons) from the AngelBot trading journal. Use when asked for a report, summary or review of the bot's trades. Read-only; works with any agent that can run Python.
---

# AngelBot performance report

Turns the bot's closed trades into **one self-contained HTML file**: no network, no CDN, no special tools, and
**no JavaScript**. Every figure and chart is already in the HTML, so it shows its data in a phone's file preview,
a mail client, a browser with scripts off, or a printout.
Read-only analytics: never place orders, never read credentials (`.env`), never edit trading config.

Files in this folder:
- `build_report.py`: reads trades, works out every figure, writes the report. Python 3 standard library only.
- `template.html`: the page. `build_report.py` fills it in. Keep it next to the script.

## 1. Get the trades

The journal is a SQLite file, `runtime/journal.db`, on the machine that runs the bot.

- **File reachable** (you are on that machine, or have a copy):
  `python3 build_report.py --db runtime/journal.db --timeframe 1-minute --out report.html`
- **Only a query tool** (for example an MCP `journal_query` tool): get the query with
  `python3 build_report.py --print-sql`, replace `:since` and `:until` with dates (for example `'0000-00-00'`
  and `'9999-99-99'` for everything), run it (limit 1000 rows), save the returned rows **unchanged** as
  `rows.json` (a list, or `{"rows": [...]}`), then run
  `python3 build_report.py --json rows.json --timeframe 1-minute --out report.html`.
  Cross-check against `SELECT COUNT(*), ROUND(SUM(net_pnl),2), ROUND(SUM(charges),2) FROM trades`: the count,
  net and fees printed by the script must match. If they do not, redo the copy.

Options: `--since YYYY-MM-DD`, `--until YYYY-MM-DD`, `--source session|replay|all` (default `all`),
`--timeframe` (the candle size the bot used, taken from its config `strategy.timeframe_min`), `--out`.

Each trade is labelled `session` (its day has a real bot start) or `replay` (everything else). The journal's
`mode` column says `paper` for both, so it cannot tell them apart. The report names its basis: `session`,
`replay` or `mixed`.

No matching trades: say so and stop. Do not write an empty report.

## 2. Optional setup-status card

Pass `--setup status.json`: a list of `{"item", "state", "detail"}` where `state` is `ok`, `warn` or `off`.
Examples: start timer active and next run time, alert service active, trading mode, candle data range, whether a
real (non-replay) session has ever run, live trading off. Only include what you actually checked. Without the
file the card is simply left out.

## 3. Hand over

Give the user the path of `report.html` (it opens in any browser and prints cleanly) and, in a few lines:
- the period, number of trades, and the basis (`session`, `replay` or `mixed`);
- the headline figures from the script's output: net after fees, win rate, profit factor, max drawdown, fees.
  Never report "profit" alone: charges count;
- fewer than 100 trades is too few to judge a strategy, and replay results prove nothing about live trading.
  Say so;
- the file is a saved copy, not live: run the skill again to refresh it.

## Rules

- Read-only. No orders, no credentials, no config edits, no restarting services.
- Do not recommend going live. Any strategy or config suggestion must be backtested on a later, unseen period first.
- Report what the numbers say, including bad news. Do not describe a result as verified unless you checked it.
- If the script or the cross-check fails, report the failure instead of hand-editing figures.

## What the report shows

Six headline cards (net result, trades, win rate, profit factor, max drawdown, fees); the running net result as a
line chart; net result per day as bars; gross versus fees versus net; a table by exit reason; a short plain-language
reading chosen from the numbers; and the optional setup-status card. Light and dark themes follow the viewer's
system setting.
