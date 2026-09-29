# ml/validation

- `purged_walk_forward`: expanding-window folds; training is purged of samples whose label window reaches the test block and embargoed further, so max(train) + horizon + embargo < min(test).
- `deflated_sharpe`: Bailey & López de Prado (2014), checked against the paper's worked example.
- `pbo`: probability of backtest overfitting by CSCV, Bailey, Borwein, López de Prado & Zhu (2017).
- `walkforward.py`: `just walkforward <name>`; evaluates a config grid on identical folds, records every trial, writes a report with Assumptions first, and registers the best config as a challenger. By default everything goes to the gitignored `out/walkforward/<name>/`, replaced on each run; only `just walkforward <name> --record` writes `backtest/reports/<name>/<date>.md` and the canonical registry. The exit code is 0 whether the verdict is PASS or FAIL; read the verdict in the report.

Regime stability, cost sensitivity, and champion comparison are not implemented yet.
