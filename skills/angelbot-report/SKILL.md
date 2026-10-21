---
name: angelbot-report
description: Build a standalone HTML report with a Today tab (today's trades and what the bot is doing now) and a Month tab (month-to-date results) from the AngelBot trading journal. Use when asked for a report, summary or review of the bot's trades. Read-only; works with any agent that can run Python.
---

# AngelBot performance report

Turns the bot's closed trades into **one self-contained HTML file**: no network, no CDN, no special tools, and
**no JavaScript needed**. Every figure and chart is already in the HTML, so it shows its data in a phone's file preview,
a mail client, a browser with scripts off, or a printout.

Two tabs: **Today** (the main one: today's trades, running result, and a "Right now" card) and **Month**
(month to date: results, daily bars, fees, exit reasons). Tabs are a small extra: without scripts both are
simply shown one after the other.

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

Options: `--since YYYY-MM-DD` (where the Month tab starts; default the 1st of this month), `--today YYYY-MM-DD`
(default: today in IST; mainly for testing), `--source session|replay|all` (default `all`), `--timeframe` (the
candle size the bot used, taken from its config `strategy.timeframe_min`), `--status runtime/status.json`
(adds the Today tab's "Right now" card), `--out`. The query returns every trade; the script keeps what the two
tabs need, so with `--json` pass rows from the start of the month at least.

Each trade is labelled `session` (its day has a real bot start) or `replay` (everything else). The journal's
`mode` column says `paper` for both, so it cannot tell them apart. The report names its basis: `session`,
`replay` or `mixed`.

No trades from the start date on: say so and stop. A day with no trades is fine: the Today tab then says
"No closed trades today yet".

## 2. Optional cards

`--status` (the bot's `runtime/status.json`) adds "Right now": whether the bot is running, open positions, a
halt or cooldown, paused entries, and a warning when the last status came from a replay. Absent file, absent card.

`--setup setup.json` adds a "Setup status" card to the Month tab: a list of `{"item", "state", "detail"}` where `state` is `ok`, `warn` or `off`.
Examples: start timer active and next run time, alert service active, trading mode, candle data range, whether a
real (non-replay) session has ever run, live trading off. Only include what you actually checked. Without the
file the card is simply left out.

## 3. Hand over

Give the user the path of `report.html` (it opens in any browser and prints cleanly) and, in a few lines:
- today's and the month's trade counts and the basis (`session`, `replay` or `mixed`);
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

**Today:** six headline cards for today's trades, the running net result, a table of today's trades (time, side,
prices, net, how each ended), a plain reading, and the optional "Right now" card.

**Month:** the same six cards for the month so far, the running net result, net result per day as bars, gross
versus fees versus net, a table by exit reason, a plain reading, and the optional setup card.

Light and dark themes follow the viewer's system setting.
