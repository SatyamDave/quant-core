> **Origin:** condensed by the coordinating agent from a prompt the repo owner pasted in chat on 2026-09-27; not the verbatim prompt. The full text is not in the repository.
>
> **Status:** ADR-0013 (Proposed) merged in #2. L0-L3 are deferred until a validated signal and labels exist; nothing in the repository learns online.

# quant-core self-learning model spec (from the repo owner; runs after bootstrap, autonomy, decision-layer Phase 6)

Principle: **Learning is continuous. Authority is bounded.** Model may change what it predicts on its own; never how much it may risk. Fast learning inside hard bounds. Structural change only via challenger -> shadow -> human approval. Getting worse always triggers automatic demotion.
Rules: plan in docs/plans/0004-self-learning-model.md; one phase per PR + stop; verify every library/API against current docs; never touch protected zones without human approval.

## Phase 1 — ADR-0013 learning speeds (docs/adr/0013-learning-speeds.md)
| Level | Speed | Adapts | Runs | Authority |
| L0 online | every event/second | vol + spread estimates, calibration offset, quote skew, feature normalization stats | inside engine, deterministic Rust | only within bounds in config/limits/adaptation.yaml (protected); resets to defaults on anomaly |
| L1 daily retrain | nightly | weights on rolling window, warm-started | loop-models offline | challenger only |
| L2 structural search | weekly | features, horizons, hyperparameters, model family | loop-discovery offline sandbox | challengers; every trial counted |
| L3 meta-learning | continuous, offline-trained | when to trust which model: regime detection, meta-labeling, ensemble weights | offline training, bounded online weighting | can only reduce exposure on its own; increase requires promotion |
Invariants: no level modifies risk limits, position caps, kill switch; every adaptation logged and replayable bit-for-bit; demotion to no signal always automatic, promotion never.

## Phase 2 — L0 in engine/crates/adapt/
Deterministic formulas only (EWMA vol, exponentially weighted calibration correction, bounded Kalman-style); no online NN updates in hot path. State updates pure functions of logged events; snapshots to event log every N s; adapted state after `just replay` equals live snapshot. Every parameter has min, max, max-change-per-minute in config/limits/adaptation.yaml (protected); at bound: clamp + alert, never extend. Anomaly reset to safe defaults + alert on stale data / sequence gap, NaN/inf, parameter pinned at bound > T, realized-vs-expected edge outside band. Tests: property (no input escapes bounds), replay-equality, chaos garbage feeds. CLAUDE.md lists allowed update rules and "adding a new adaptive parameter requires a bound in config/limits and a human approval."

## Phase 3 — ml/execution/ (learn from own trading)
Models: fill probability P(fill | level, queue position est, book state, time-in-queue); adverse selection (mid move after our fill per horizon, by fill type); markout (realized-edge distribution per strategy/venue/regime); latency (real distribution feeding backtest latency models). Selection-bias controls: log every quoting decision incl. not taken with probabilities at decision time; importance weighting + off-policy evaluation; exploration = small randomized quote perturbation only inside budget in config/limits/exploration.yaml (protected, default OFF), own human approval, daily cost cap, every exploratory order tagged. Simulator honesty: these calibrate backtest/fill_models + latency_models; monthly backtest-vs-live fill gap tracked, shrinking it a standing backlog item.

## Phase 4 — ml/training/continual/ (L1)
Nightly rolling window, length chosen by walk-forward and recorded; warm start from champion plus from-scratch control. Labels: ternary up/flat/down net of costs; execution-aware second target. Forgetting controls: replay buffer of past regimes esp. stress; regime panel (calm, trending, high-vol, liquidation cascades, low liquidity); challenger improving recent but collapsing on stress fails. Recalibrate every retrain on latest held-out fold, reliability per regime. Output registered challenger (dataset hash, feature version, config, metrics, lineage); nothing promoted.

## Phase 5 — ml/search/ (L2)
Search space: features (library + RD-Agent/qlib proposals), label horizons, abstention thresholds, hyperparameters, model family (linear -> GBDT -> small sequence). Optuna or equivalent, fixed weekly trial budget. Every trial in registry; cumulative trial count feeds deflated Sharpe + PBO (bar rises automatically). Feature lifecycle candidate -> validated -> production -> retired; decaying importance / unstable sign retired via T2 PR; retired to graveyard with tags. Complexity penalty: beat simpler champion after costs by policy margin AND fit Rust latency budget. Every new production feature needs Rust impl + parity test before shadow.

## Phase 6 — ml/meta/ (L3)
Regime model (HMM/clustering + rules over vol, spread, volume, funding, cross-venue dispersion) tags periods; metrics per regime. Meta-labeling: secondary classifier "will primary signal's trade be profitable after costs given context?" gates/sizes DOWNWARD only. Ensemble: contextual bandit or Bayesian updating over approved models by recent realized edge per regime; may shift allocation and shrink total exposure, never raise above approved cap. Self-doubt: confidence shrunk toward abstention on OOD detection, ensemble disagreement, calibration drift.

## Phase 7 — closed loop (loop-models, ml/monitoring/)
Monitors per model: feature drift (PSI/KS), prediction drift, rolling ECE, realized vs expected edge, markout decay, abstention rate, regime mix. Always-allowed actions: soft degrade (raise abstention threshold, shrink meta-label size); hard demote (no signal, roll back to previous champion if healthy); record to registry + needs-human issue with diagnostic dossier. Promotion ladder never automatic: validation + regime panel + trial-adjusted gates -> shadow >= N days/events vs champion -> promotion-dossier (metrics, regime panel, drift, execution-aware PnL estimate, diff vs champion, risks) -> human T3 -> canary small fraction with auto rollback -> full allocation within existing budget. Every demotion / failed shadow / canary rollback -> registry record, graveyard or journal entry, eval case; repeated patterns into ml/CLAUDE.md Learned + search-space config via knowledge PRs.

## Phase 8 — docs
ml/CLAUDE.md (four speeds + what each may change; demotion automatic, promotion human; trial accounting mandatory; regime panel required; online bounds in protected config). adapt CLAUDE.md. Skills: diagnose-model-decay, add-feature, regime-panel-report, exploration-proposal. Glossary: online learning, catastrophic forgetting, replay buffer, meta-labeling, regime, contextual bandit, off-policy evaluation, markout, adverse selection, PSI, ECE. how-this-repo-learns.md "how the model learns" with L0-L3 diagram and demotion/promotion paths.

## Acceptance
ADR-0013 approved; adaptation.yaml + exploration.yaml exist, protected, exploration off; property tests L0 in bounds; replay reproduces adapted state bit-for-bit; NaN/stale/pinned-bound reset + alert tests; fill-prob + adverse-selection train from sample logs with OPE; nightly retrain registers challenger with regime panel, stress-failing challenger rejected; every search trial in registry, deflated Sharpe uses cumulative count (test); meta/bandit can't exceed approved cap (property test); injected drift -> soft degrade -> hard demote -> rollback -> needs-human issue; promotion without human approval blocked at every step (test); weekly report shows champion age, challenger win rate, demotions, backtest-vs-live fill gap, trial counts.

## Do NOT
No level modifies risk limits, position caps, exploration budgets, kill switch. No non-replayable online updates. No auto-promotion past shadow. No hidden trials. No training on live labels without Phase 3 selection-bias controls. Never loosen a bound/threshold to make a model pass; stop and ask.
