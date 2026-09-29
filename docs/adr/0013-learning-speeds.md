# ADR-0013: Learning speeds (L0-L3) and their authority

## Status

Proposed (2026-09-27). Needs human approval (T3) before any Phase 2-8 work in
a self-learning plan (see `docs/specs/0004-self-learning.md`) begins.

## Context

The root rules say that models retrain and challenge each other automatically, and
that promotion to real capital always passes statistical gates and human approval
(bootstrap spec §1, §9). ADR-0012 (decision-layer Phase 6) defines the in-house
signal model: a calibrated selective classifier with ternary up/flat/down labels,
exported to deterministic Rust inference, that emits "no signal" when its confidence
is low. `loop-models` and `loop-discovery` (autonomy Phase B) already exist to
retrain challengers and run sandboxed discovery offline.

"Self-learning" still covers several very different things. Updating a volatility
estimate every second, refitting weights overnight, searching for new features, and
deciding which model to trust in a given regime each carry different risks, run in
different places and need different controls. Without a written split, any one of
them could quietly take on authority that belongs to another, for example an online
update that also widens exposure, or a search that runs trials no one counts.

This ADR fixes that split. The principle: **learning is continuous, authority is
bounded.** A model may change what it predicts on its own. It never changes how much
it may risk.

## Decision

### Four learning speeds

| Level | Speed | Adapts | Runs | Authority |
|---|---|---|---|---|
| L0 online | every event / second | volatility and spread estimates, calibration offset, quote skew, feature normalization statistics | inside the engine (`engine/crates/adapt/`), deterministic Rust | only within the bounds in `config/limits/adaptation.yaml` (protected); resets to defaults on anomaly |
| L1 daily retrain | nightly | model weights on a rolling window, warm-started from the champion | `loop-models`, offline | produces challengers only |
| L2 structural search | weekly | features, label horizons, abstention thresholds, hyperparameters, model family | `loop-discovery` / `ml/search/`, offline sandbox | produces challengers only; every trial is counted |
| L3 meta-learning | continuous, trained offline | when to trust which model: regime detection, meta-labeling, ensemble weights | trained offline; bounded online weighting in the engine | may reduce exposure on its own; any increase requires promotion |

### Global invariants (apply to every level)

1. No level modifies risk limits, position caps, exploration budgets or the kill
   switch. These live in `engine/crates/risk/**` and `config/limits/**`, and only
   humans change them (root rule 3, rule 4).
2. Every adaptation is logged and replayable bit-for-bit. An update that cannot be
   reproduced from the event log and the recorded inputs is not allowed to run.
3. Demotion to "no signal" is always automatic and always allowed. Promotion is
   never automatic: past shadow it is a T3 human decision (autonomy spec, Phase A).
4. A bound is never loosened to let a model pass. When a model only passes under a
   looser bound or threshold, the work stops and a human is asked (root rule 12).
5. Every trial at every level is recorded in the experiment registry, including
   failures (root rule 6). Hidden trials would make the overfitting defense below
   meaningless.
6. The risk crate checks every order regardless of what any learner outputs.
7. No training on live execution labels without the selection-bias controls below.

### What each level may change

**L0 online.** It may change exactly these engine-local parameters: volatility
estimate, spread estimate, calibration offset, quote skew and feature normalization
statistics (running mean and variance per feature). Adding a parameter to this list
requires a bound in `config/limits/adaptation.yaml` and a T3 human approval.
- Allowed update rules: EWMA, exponentially weighted calibration correction, and
  bounded Kalman-style filters. No online neural-network updates in the hot path.
- Bound source: every parameter has `min`, `max`, `max_change_per_minute` and a safe
  `default` in `config/limits/adaptation.yaml`. At a bound the value is clamped and
  an alert fires; the bound is never extended at runtime.
- Anomaly reset: stale data or a sequence gap, NaN or infinity, a parameter pinned
  at a bound for longer than the configured time, or realized-vs-expected edge
  outside its band resets every L0 parameter to its default and alerts.
- Replay: each update is a pure function of logged events and the previous state.
  Adapted state is snapshotted to the event log every N seconds (N set in
  `adaptation.yaml`), and the state after `just replay` must equal the live snapshot
  bit-for-bit. A replay mismatch blocks merge (root rule 7).
- Authority: L0 cannot promote anything. It can only move within bounds or fall
  back to defaults. Changing a bound or default is a T3 change to a protected file.

**L1 daily retrain.** It may change the weights of a model whose feature set,
label horizon, family and hyperparameters are fixed by the current champion's
registry entry. Anything else is L2.
- Bound source: L1 touches no live state, so it has no runtime bound file. Its
  bounds are the champion's registered configuration and the validation gates. The
  rolling-window length is chosen by walk-forward and recorded, not tuned to pass.
- Replay: training runs on CPU with fixed seeds, pinned library versions from the
  lockfile, and deterministic modes where the library offers them (for example
  LightGBM's `deterministic=true`, which the LightGBM parameter docs say applies to
  the CPU device only and still differs across versions and builds). The registry
  entry records dataset hash, feature version, config, seed and library versions,
  so the same inputs reproduce the same artifact hash.
- Authority: a retrain produces a registered challenger. A challenger that fails
  the stress regimes of the regime panel is rejected automatically. Entering shadow
  (live predictions, no orders) may be automatic once validation gates pass; every
  step past shadow is T3.

**L2 structural search.** It may change the feature set (from the feature library
and sandbox proposals), label horizons, abstention thresholds, hyperparameters and
model family (linear, then gradient-boosted trees, then small sequence models).
- Bound source: the search space and the fixed weekly trial budget live in
  `ml/search/` config (T2). A new production feature also needs a Rust
  implementation and a Python/Rust parity test before shadow. A model family or
  feature must beat the simpler champion after costs by the policy margin and fit
  the Rust latency budget.
- Replay: each trial is logged with its sampled parameters, seed and dataset hash
  and is individually reproducible. The search trajectory is reproducible only when
  trials run sequentially with a seeded sampler; Optuna's FAQ states that parallel
  or distributed optimization is inherently non-deterministic. Parallel search is
  allowed, but the trial count and every trial's inputs are always recorded.
- Authority: produces challengers only, on the same ladder as L1. Retiring a
  production feature is a T2 PR; retired features go to the graveyard with tags.

**L3 meta-learning.** It may change regime tags, the meta-label gate and size
multiplier, ensemble weights across already-approved models, and a confidence
shrinkage toward abstention.
- Bound source: the online part (ensemble weights, meta-label size multiplier,
  confidence shrinkage) is bounded by an L3 section in
  `config/limits/adaptation.yaml`: the size multiplier is in [0, 1], ensemble
  weights sum to at most 1 of the approved allocation, and each has a
  `max_change_per_minute`. Nothing L3 does can raise exposure above the approved cap.
- Replay: offline training follows the L1 rules. The online weighting is a
  deterministic update in the engine, logged and replayed exactly like L0.
- Authority: L3 may reduce exposure or abstain on its own (on out-of-distribution
  detection, ensemble disagreement or calibration drift). It cannot add a model to
  the ensemble or raise exposure; both need promotion through the T3 ladder.

### Exploration budget

Learning from our own fills is biased because we only see outcomes of the quotes we
chose (see below). The only deliberate way to widen that data is a small randomized
quote perturbation. It is not a learning level and has its own bound file:
`config/limits/exploration.yaml` (protected). Its default is **off**. Turning it on
needs its own T3 approval, runs inside a daily cost cap set in that file, and tags
every exploratory order. No level may change this file.

### Authority summary

| Level | May change on its own | Bound source | Replay requirement | Demotion | Promotion |
|---|---|---|---|---|---|
| L0 | vol, spread, calibration offset, quote skew, normalization stats | `config/limits/adaptation.yaml` (protected) | pure function of logged events; `just replay` state equals live snapshot bit-for-bit | automatic reset to defaults on anomaly | none; new parameter or bound change is T3 |
| L1 | weights of the champion's fixed configuration, as a challenger | champion's registry config + validation gates | dataset hash, config, seed, pinned versions reproduce the artifact hash | automatic rejection (including stress-panel failure) | shadow automatic after gates; past shadow T3 |
| L2 | features, horizons, abstention thresholds, hyperparameters, family, as challengers | `ml/search/` space and weekly trial budget (T2); gates | every trial logged and individually reproducible | automatic rejection; feature retirement via T2 PR | shadow automatic after gates; past shadow T3 |
| L3 | regime tags, meta-label size in [0, 1], ensemble weights, abstention shrinkage | L3 section of `config/limits/adaptation.yaml` (protected) | offline as L1; online weighting replayed as L0 | automatic, may reduce exposure or abstain at any time | any exposure increase or ensemble change is T3 |
| Exploration | nothing on its own | `config/limits/exploration.yaml` (protected, default off) | exploratory orders tagged and logged | automatic off on cost cap | enabling is its own T3 |

### Overfitting defense

L1 and L2 run many trials by design, and the best of many trials looks good by
chance. Two measures from Bailey and López de Prado correct for this, and both use
the **cumulative** trial count across all levels and weeks, read from the registry:

- Deflated Sharpe ratio (DSR): corrects an observed Sharpe ratio for the number of
  trials, their variance, and non-normal returns. Bailey, D. H. and López de Prado,
  M. (2014). "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
  Overfitting, and Non-Normality." *The Journal of Portfolio Management* 40(5),
  94-107. doi:10.3905/jpm.2014.40.5.094.
- Probability of backtest overfitting (PBO), estimated with combinatorially
  symmetric cross-validation. Bailey, D. H., Borwein, J., López de Prado, M. and
  Zhu, Q. J. (2017). "The Probability of Backtest Overfitting." *The Journal of
  Computational Finance* 20(4), 39-69. doi:10.21314/JCF.2016.322.

Because the trial count only grows, the bar a challenger must clear rises
automatically as the search runs longer. A test in Phase 5 proves that DSR uses the
cumulative count rather than the count for one study.

### Selection bias in own-execution data

Fill-probability, adverse-selection and markout models (Phase 3) learn from our own
orders. We only observe outcomes for quotes we placed, and our quoting policy chose
those quotes, so a model trained naively on fills learns our policy's blind spots.
Controls:
- Log every quoting decision, including quotes considered and not placed, with the
  policy's probabilities at decision time.
- Estimate the value of a different policy with off-policy evaluation: importance
  weighting by the logged propensities, and the doubly robust estimator, which
  combines a reward model with importance weights and stays unbiased if either one
  is correct. Dudík, M., Erhan, D., Langford, J. and Li, L. (2014). "Doubly Robust
  Policy Evaluation and Optimization." *Statistical Science* 29(4), 485-511.
  doi:10.1214/14-STS500.
- Widen coverage only through the exploration budget above.

### Libraries named in this plan

Versions and licenses as checked on PyPI and GitHub on 2026-09-27. They are
candidates to validate in the phase that uses them, not commitments; each gets an
ADR-0002 dependency entry when it is added.

| Library | Use | Latest version (date) | License | Notes |
|---|---|---|---|---|
| [Optuna](https://github.com/optuna/optuna) | L2 search | 5.0.0 (2026-09-07) | MIT | Seeded samplers; parallel runs non-deterministic (FAQ) |
| [LightGBM](https://github.com/lightgbm-org/LightGBM) | L1/L2 GBDT | 4.7.0 (2026-07-18) | MIT | Repository moved from microsoft/ to lightgbm-org/ |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | Python-side parity checks of ONNX exports (ADR-0012) | 1.30.0 (2026-09-10) | MIT | |
| [hmmlearn](https://github.com/hmmlearn/hmmlearn) | L3 regime HMM | 0.3.3 (2024-10-31) | BSD-3-Clause | README states limited-maintenance mode |
| [scikit-learn](https://github.com/scikit-learn/scikit-learn) | L3 regime clustering alternative (e.g. Gaussian mixtures), calibration | 1.9.1 (2026-09-10) | BSD-3-Clause | Fallback if hmmlearn's maintenance status rules it out |

## Alternatives

- **One learning loop with a single approval step.** Simpler, but it treats a
  per-second spread estimate and a new model family the same way. Either the fast
  path waits for humans, which makes online adaptation impossible, or structural
  changes get the fast path's autonomy. Rejected.
- **Online learning of model weights in the engine.** Faster reaction, but weight
  updates in the hot path are hard to bound, hard to replay exactly, and move the
  decision about what the model is out of the registry. Rejected; L0 is limited to
  the listed deterministic estimators.
- **No online adaptation (everything nightly).** Safest to reason about, but
  volatility and spreads move intraday and a stale quote skew is its own risk.
  Rejected in favor of bounded L0.
- **Let L3 raise exposure when confidence is high.** Would let a meta-model add
  risk without human review. Rejected; L3 may only reduce.
- **Put search budgets and L1 bounds in protected config.** Would make every search
  tweak a two-human change. Rejected for L1/L2 because they touch no live state and
  pass through the T3 ladder anyway; kept protected for L0, L3 online weights and
  exploration, which do touch live quoting.

## Consequences

- `config/limits/adaptation.yaml` and `config/limits/exploration.yaml` become
  protected files (bootstrap already protects `config/limits/**`). Adding an
  adaptive parameter or an L3 online bound is a T3 change.
- Phase 2 must prove with property tests that no input moves an L0 or L3 online
  value outside its bounds, and with a replay test that adapted state is identical.
- The registry must store the cumulative trial count and every trial; DSR and PBO
  read from it. A challenger's gate bar is a function of that count.
- Execution models cannot train on live labels until Phase 3's decision logging and
  off-policy evaluation exist.
- The engine gains a new crate (`engine/crates/adapt/`), which is non-protected
  engine code (T2) but reads bounds only from protected config.
- Reaction to regime changes is limited to what bounded L0 and downward-only L3
  can do; anything faster or larger waits for a human. That is intended.
- hmmlearn's limited-maintenance status is a known dependency risk for Phase 6.

## Review date

2027-03-27, or earlier when Phase 7 records its first automatic demotion or when
any level needs authority this ADR does not grant.
