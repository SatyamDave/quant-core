# Research cycle

One bounded cycle per question. Source: the owner's research-loop prompt (2026-09-27) §5, §6, §8.
A cycle may end in REJECT or INSUFFICIENT EVIDENCE. Never manufacture a winner.

## Where things live

- Study folder: `research/studies/NNNN-<slug>/`, one per cycle (first: `0001-favorite-longshot`).
  `REGISTRATION.md` (prose) and `config.json` (frozen; wins on conflict) are committed before any
  outcome is fetched. Raw requests go to `data/raw/study-NNNN/requests.jsonl`. The manifest and
  report names below are this doc's proposal until study 0001 adds its own.
- Registry: `research/registry/` (append-only JSONL; `record()`, `trial_count()`). Every trial,
  failures included (root rule 6).
- Graveyard: `docs/research/graveyard.md`. Check it before step A; add a row at step I on rejection.
- Checks: `just check` before every push. `just replay` (synthetic fixture, committed hash) and
  `just walkforward demo` (scratch output in `out/`) run; `just backtest` is not implemented and exits 2. A study records its own exact command in its manifest.

## Checklist

**A. Select one uncertainty**
- [ ] Mechanism, why it exists, why competitors have not removed it, and the observation that falsifies it.
- [ ] Say which it is: forecasting skill, execution quality, or software reliability.
- [ ] Ranked by decision value against cost. The first cycle runs one primary experiment.

**B. Register before measuring the outcome** (`REGISTRATION.md` + `config.json`, committed before results)
- [ ] Hypothesis, baseline, dataset with availability times, target and label horizon, split rules.
- [ ] Candidate search budget, primary metric, minimum useful effect (from task economics, not a copied target).
- [ ] Evaluation method, failure criteria, compute limit, decision rule.
- [ ] Config frozen. Any later change is a new trial in the registry.

**C. Validate data**
- [ ] Event, publication, first-available and ingestion times; revisions; when the final label is known.
- [ ] Only information available at the simulated decision time. Preprocessing, features, selection
      and calibration are fit inside training partitions.
- [ ] Missing rows, duplicates, sequence gaps, clock skew, selection effects, survivor bias.
- [ ] Event contracts: keep the settlement rule and its version. Contracts on one event are not independent.

**D. Baseline**
- [ ] Simple rule, no-trade, market-implied forecast, or a conventional model, with the same
      information, exposure limits and costs as the candidate. Explain why it is fair.
- [ ] A reliable data capture plus a baseline report is a valid first output.

**E. Limited candidate comparison**
- [ ] One mechanism at a time, with an ablation.
- [ ] `manifest.json` stores: code revision, dependency lock hash, dataset fingerprint, split
      manifest, seed, model or prompt version, feature config, runtime, cost, outputs, failures.
- [ ] Hosted model calls are not bit-reproducible: save requests and responses (within
      docs/security/data-classes.md once merged) and keep replays separate from new calls.

**F. Evaluate without leakage**
- [ ] Chronological splits, overlapping labels purged, a justified gap. Tuning data is separate
      from the final held-out set; once the held-out set informed a change it is no longer untouched.
- [ ] Count the full search (`trial_count`). Statistics fit the sample size and dependence; report
      uncertainty and effective independent observations. Deflated Sharpe only where its assumptions fit.
- [ ] Retrospective LLM forecasts are exploratory only; prospective timestamped predictions are primary.

**G. Execution economics**
- [ ] After fees, spread, latency, slippage, partial fills, queue uncertainty, impact, adverse selection,
      capital limits. Touching a limit price is not a fill; candles cannot show queue-sensitive execution.
- [ ] Downgrade any claim the data cannot support. Show trading economics and research/operating cost separately.

**H. Verify independently**
- [ ] Re-run from the saved manifest in a clean environment and compare outputs.
- [ ] Separate review of accounting, sample construction, baseline fairness and artifacts.
- [ ] The candidate cannot edit the evaluator or read held-out labels. Another model agreeing is not evidence.

**I. Decide and preserve** (`report.md`)
- [ ] PROMOTE TO NEXT STAGE, REJECT, or INSUFFICIENT EVIDENCE, with the reason.
- [ ] Negative result goes to the graveyard with revisit conditions.
- [ ] One concrete next action: stop, repair data, collect labels, test a different mechanism, or begin shadow.
- [ ] Do not keep searching the same data until something passes.

Stages after a cycle: offline research → forward shadow → sandbox integration → explicitly
authorized limited live → reviewed scaling. Live trading, new capital, paid services and
deployment are not authorized by any cycle.

## What counts as learning

- **Research memory:** findings and rejections with sources, scope, date and recheck conditions.
  Memory notes never train weights.
- **Predictive learning:** versioned features, classifiers and calibration fit on legitimate labels.
- **Agent improvement:** tools, prompts and routing changes backed by task evals.
- **Execution improvement:** measured fills, costs, latency and reconciliation.

Self-critique is not ground truth, and more iterations are not improvement. Retraining produces a
challenger, never authority to raise risk or deploy. Drift triggers an investigation under a
predefined policy. Labels arrive late and counterfactuals are missing; labeling only executed
trades is selection bias. No real-money exploration to collect data. Offline updates and shadow
first; RL and online policy adaptation are deferred.

## Every PR

The body states: **problem**, **change**, **evidence** (commands run and their results), **limitations**,
and **rollback or removal path**. A rigorous negative finding is a valid PR.
Progress is judged by hard correctness and risk invariants plus statistically justified tolerances
under comparable conditions, not by test counts, raw coverage, or a lucky benchmark peak. No bot
rewrites evaluation thresholds or blesses a result it optimized.
