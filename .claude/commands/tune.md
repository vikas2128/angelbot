---
description: Walk-forward parameter tuning for a strategy
argument-hint: <SYMBOL> <strategy>
---
Tune $ARGUMENTS with walk-forward validation.

1. `list_strategies` for defaults; `list_data` for the date range.
2. Define a SMALL grid around defaults (≤ 30 combos). Explain the grid.
3. `param_sweep` on the older ~70% (in-sample). Take the top 3 by net P&L that also have
   ≥ 30 trades and profit factor > 1.
4. `run_backtest` each of those 3 on the newer ~30% (out-of-sample), plus current config params.
5. Recommend a change ONLY if it beats current params out-of-sample on net P&L AND max DD,
   and stability looks reasonable (neighbouring params also OK). Otherwise say "keep current".
6. If recommending, show the exact YAML diff and ask before editing `config/config.yaml`;
   after editing run `validate_config`.
