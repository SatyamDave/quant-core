# Writing a strategy

A strategy is a folder under `strategies/<name>/` that moves through fixed lifecycle gates.
Nothing reaches paper or real money without passing the gate before it.

```
idea → research → backtest → walk-forward → paper → canary (tiny capital) → scaled
```

Skipping a gate is a blocking error. The thresholds for each gate are in
`strategies/_template/GATES.md`; a strategy may set stricter thresholds, never looser ones.

## 1. Start from the template

```sh
cp -r strategies/_template strategies/my_strategy
```

Or ask Claude Code to use the `new-strategy` skill. Fill in every field of the README: thesis,
edge source, why it persists, capacity and kill criteria. A blank field fails the research gate.
`config.yaml` holds strategy parameters only. Risk limits never live there; they come from
`config/limits/` and can only be tightened at runtime.

## 2. Check the graveyard

Read `docs/research/graveyard.md` and the experiment registry (`research/registry/`) first. If
something close already failed, say why this attempt is different.

## 3. Research and backtest

Follow `docs/process/research-cycle.md`: register the study before measuring, compare against a
baseline, keep evaluation leakage-free, and have the result re-run independently. Every run is
recorded in the registry, including failures. An unrecorded backtest does not count.

## 4. Walk-forward

`just walkforward <name>` runs out-of-sample folds. The gate checks deflated Sharpe with the true
trial count, probability of backtest overfitting, the share of profitable folds and drawdown.

## 5. Paper

Paper trading runs the full loop against simulated or broker-paper fills. Every paper day must
replay to identical orders (`just replay`). Record each gate change in the README's gate history.

## 6. Canary and scaled

Canary uses tiny capital with tight limits and requires an explicit operator approval recorded
in the README. Scaling up needs the canary thresholds met and another approval. Daily PnL is
reconciled against the venue's statements.

## Letting an agent decide

A strategy can supply signals to the agent service's decision request, and the agent decides
`submit_order_intent` or `no_trade`. The decision still goes through `qc-bridge` and the risk
gate, and the kill switch still wins. See `agent/README.md` and
[ADR-0040](../adr/0040-agentic-decision.md).

## When a strategy fails

Move it to the graveyard with the reason and the registry link, so nobody retries it blindly.
A rejected idea is a valid, useful result.
