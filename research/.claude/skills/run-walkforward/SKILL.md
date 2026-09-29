---
name: run-walkforward
description: Run walk-forward validation for a strategy or model. Use when validating, evaluating, or testing out-of-sample performance in research/ or ml/.
---
## Steps
1. Confirm the idea is not in docs/research/graveyard.md. If similar, stop and report.
2. Load the config from backtest/configs/<name>.yaml; refuse if fees, latency, or fill model are missing.
3. Run `just walkforward <name>` (purged + embargoed folds, fixed seeds).
4. Compute: net Sharpe after costs, deflated Sharpe with the TRUE number of trials from the registry, max drawdown, turnover, capacity estimate, PBO.
5. Log the run to the experiment registry (including failures).
6. Write backtest/reports/<name>/<date>.md with an Assumptions section first.
7. Verdict: PASS only if every gate in strategies/_template/GATES.md is met; otherwise add a graveyard entry with the reason.
