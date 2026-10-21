---
description: Build the AngelBot performance report (a dashboard) from the journal
argument-hint: [since YYYY-MM-DD] [until YYYY-MM-DD] [session|replay|all]
---
Build the performance report for: $ARGUMENTS (default: the whole journal, all trades).
Read-only analytics: never edit config, never place orders, never read `.env`.

Files for this command live in `.claude/report/`:
- `build_data.py`: turns closed trades into the CSV datasets and prints a summary to cross-check.
- `index.html`: the dashboard page (charts, tables and wording all come from the datasets).

## 1. Get the numbers

Work in the scratchpad directory. `source` marks each trade `session` (a day with a real bot start)
or `replay` (anything else); `mode` alone cannot tell them apart. Say which one the report is about.

- **Journal file reachable** (Claude Code on the VM, or a copied `journal.db`):
  `./.venv/bin/python .claude/report/build_data.py --db runtime/journal.db --out <scratch>/report [--since D] [--until D] [--source session|replay|all] --timeframe "<N>-minute"`
- **Only the MCP tools** (laptop to VM): run `journal_query` with the `SQL` constant from
  `build_data.py` (fill `:since` / `:until`, or use the whole range; `limit` 1000), write the returned
  `rows` unchanged to `<scratch>/report/rows.json`, then run the script with `--json` instead of `--db`.
  Copy values exactly. Then confirm with
  `SELECT COUNT(*), ROUND(SUM(net_pnl),2), ROUND(SUM(charges),2) FROM trades` (same filters) that the
  script's trade count, net and fees match; if not, redo the copy.
- Timeframe label: `get_config` -> `strategy.timeframe_min`.
- No trades: say so and stop. Don't publish an empty report.

## 2. Write the setup-status file

`<scratch>/report/vm.csv` with header `item,state,detail` and these rows. `state` is `ok`, `warn` or `off`.
Fill from `service_status`, `bot_status` and `list_data`:
- Morning start timer: `ok` if the timer is active, with the next run time.
- Telegram control: `ok` if the service is active, else `warn`.
- Trading mode: `ok` for paper (say simulated orders, real prices); `warn` if live.
- Candle data: file, range and row count.
- Real paper session: `warn` with "None yet. All journal activity is replays" if every journal `start` event
  says `replay=True`; otherwise `ok` with the last real session date.
- Live trading: `off` unless the status shows live (a manual step for the user).

## 3. Publish the dashboard

Use the Artifact tool. Follow the Dashboard type's own instructions that its result returns.

- **New report**: `Artifact` `action: "quickstart"` (intent `other`), then publish with the Dashboard `type_url`,
  a `title` ("AngelBot report"), no files, `auto_open: "after_first_write"`.
- **Refresh**: if the user gave a report URL, or one made earlier in this conversation exists, update that one
  (do not make another).

Then, for that artifact URL:
1. Upload `trades.csv`, `by_day.csv`, `by_reason.csv`, `totals.csv`, `vm.csv` as assets (`asset: true`,
   `file_path`; **one call per CSV**). Use each returned `/_blob/<id>` url as given.
2. One `ArtifactData` batch:
   - `dash/meta` = `{"title": "AngelBot report"}`
   - `datasets/<id>` for exactly these ids: `trades`, `by_day`, `by_reason`, `totals`, `vm`, each
     `{"title", "description", "fixed": true, "updated": {"at": <ISO now>, "by": "Claude, from the journal"},
     "source": {"kind": "file", "url": "<blob url>", "name": "<file>.csv", "format": "csv"}}`
   - `files/index.html` = `{"text": <the full text of .claude/report/index.html>}` (write it via a JSON
     file with `file_path`). On a refresh, change only the datasets' `url`, `name` and `updated`
     (use `update`), and the page only if the template changed.
3. Read `data/users/me` document `check` (at most twice). Don't render or screenshot the page.

## 4. Hand over

Give the link (private until the user shares it) and in a few lines:
- What the report covers: dates, trades, and whether the basis is `session`, `replay` or `mixed`.
- Headline figures quoted from the script's summary: net after fees, win rate, profit factor,
  max drawdown, fees. Never just "profit".
- Under 100 trades is too few to judge a strategy; say so. Replay results prove nothing about live trading.
- The report is a saved copy, not live: ask for `/report` again to refresh it.
- Say plainly if the page check had not reported, instead of calling it verified.

Do not suggest going live, and do not change any config from this command. Suggestions must be backtested first.
