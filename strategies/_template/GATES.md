# Lifecycle gates

idea → research → backtest → walk-forward → paper → canary → scaled. Skipping a gate is a blocking error (root rule 5). A strategy advances only when every threshold of its current gate is met; canary and scaled also need human approval recorded in the README's gate history. A strategy may set stricter thresholds here, never looser ones.

These are starting defaults to be reviewed by a human before the first strategy uses them.

| Gate | Threshold | Default |
|------|-----------|---------|
| idea → research | Not in `docs/research/graveyard.md`; README thesis, edge source and kill criteria filled in | required |
| research → backtest | Point-in-time data only; lookahead check passes | required |
| backtest → walk-forward | Net Sharpe after fees, funding, latency and slippage | ≥ 1.5 |
| | Trades in the sample | ≥ 500 |
| | Report has an Assumptions section and a registry link | required |
| walk-forward → paper | Deflated Sharpe (a probability in [0, 1]), using the true trial count from the registry | ≥ 1.0, unreachable in practice; threshold pending a human decision |
| | Probability of backtest overfitting (PBO) | ≤ 0.20 |
| | Out-of-sample folds with positive net PnL | ≥ 70% |
| | Max drawdown relative to backtest max drawdown | ≤ 1.5× |
| paper → canary | Days in paper | ≥ 14 |
| | Realized edge relative to walk-forward expectation | ≥ 50% |
| | Replay of every paper day reproduces its orders | 100% |
| | Human approval | required |
| canary → scaled | Days in canary | ≥ 30 |
| | Daily PnL reconciled with venue statements | 100% of days |
| | Kill-criteria or risk-limit breaches | 0 |
| | Realized edge relative to paper | ≥ 70% |
| | Human approval | required |
