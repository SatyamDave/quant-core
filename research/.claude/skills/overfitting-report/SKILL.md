---
name: overfitting-report
description: Assess overfitting in a backtest or model result. Use when a result looks too good, before promoting past walk-forward, or when asked for deflated Sharpe or PBO.
---
## Steps
1. Get the true trial count for the experiment with `registry.trial_count` (research/registry). Include every variant tried, including sandbox factors.
2. Compute deflated Sharpe with that count, and PBO via combinatorially symmetric cross-validation.
3. Check sensitivity: costs doubled, latency doubled, parameters perturbed.
4. Check regime stability across folds.
5. Write the report with an Assumptions section first; state the trial count explicitly.
