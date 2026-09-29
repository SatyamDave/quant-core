> **Origin:** condensed by the coordinating agent from a prompt the repo owner pasted in chat on 2026-09-27; not the verbatim prompt. The full text is not in the repository.
>
> **Status:** Phase 1 docs only (#4, merged 2026-09-28). Phase 5 distillation is blocked by the TypeSafe MCA §2.3(b) distillation clause recorded in #4. Phases 2-7 are deferred.

# quant-core decision-layer spec (from the repo owner; after bootstrap + autonomy A-B)

## 0. Context and decision
TypeSafe AI's Jev (as described by the owner; VERIFY every claim against typesafe.ai): a "System One" model taking a state (string or JSON) and typed questions, returning calibrated probabilities. Primitives: Choice (pick one of a set), Score (rate against defined levels), Noul (probability a statement is true). Hosted proprietary API, no weights, no self-hosting, ~70-500 ms latency, early access, self-reported benchmarks.

ADR-0010 docs/adr/0010-decision-layer.md:
1. Jev NOT used in trading engine or price prediction: microsecond tick-to-trade budget vs hundreds of ms round trip; hosted changing third-party model breaks replay determinism and root rule 1 (no AI API calls from any process holding venue credentials); not trained on our order book data; pretrained model on historical text risks lookahead leakage.
2. Jev IS used in the control plane: swarm routing; triage of learnings, CI failures, alerts; backlog dedup and scoring; research-paper relevance filtering; second opinion on autonomy tiers.
3. Trading model follows same idea in-house: calibrated selective (abstaining) classifier on our own data, exported to deterministic Rust inference, outputs probabilities, "no signal" when confidence low.
4. Every Jev use behind a provider interface; decisions logged, evaluated against alternatives, distilled into local models over time; no single-vendor dependency for a core function.

Rules: plan in docs/plans/0003-decision-layer.md first; one phase per branch/PR then stop; verify current TypeSafe docs and SDKs before any integration code (official typesafe.ai docs + API reference); never invent request schemas, field names, SDK methods, or model IDs.

## Phase 1 — vendor verification and data policy (docs only)
docs/adr/0011-typesafe-vendor-review.md:
- Access path: allowed = official api.typesafe.ai or established gateways (Vercel AI Gateway, OpenRouter, LiteLLM) if needed. NOT allowed = unofficial reseller / "no-waitlist" key sites (credential and data-leak risk).
- Model pinning: explicit version, never jev-latest, in any production path; record pinned version; monthly re-evaluation before any bump.
- Terms and data handling: retention, training-on-customer-data, region, SLA, rate limits, pricing; flag anything unclear for a human.
docs/security/data-classes.md:
| Class | Examples | May be sent to Jev? |
| Public | open-source code, public papers, public docs | Yes |
| Internal | CI logs (scrubbed), issue text, learnings, file paths, PR titles | Yes, after the scrubber |
| Secret / Alpha | API keys, strategy parameters, features, model weights, signals, PnL, positions, risk limits, venue account IDs, fund documents | Never |
Every call passes a scrubber (remove secret patterns, then per-call-site field allowlist). A call site that can't prove its payload is Public or Internal doesn't ship.

## Phase 2 — platform/decide/ (Python package, own CLAUDE.md)
decide/types.py (Choice, Score, YesNo; Decision: label, probs, confidence, provider, version, latency, cost); providers/base.py (DecisionProvider protocol), jev.py (official SDK/HTTP, pinned model, timeouts, retries w/ jitter, circuit breaker), llm.py (small Claude model, strict structured output), local.py (distilled, zero network), rules.py (deterministic, preferred when sufficient); router.py (per call site provider order + thresholds from config); scrubber.py; ledger.py (append-only Parquet: inputs hash, scrubbed input, outputs, provider, version); callsites.py (named registry; unknown = error); config/decide.yaml (policy-protected); tests/.
Confidence routing: above high threshold auto-act; between escalate to next provider or human queue; below low abstain; thresholds per call site calibrated in Phase 4, never guessed. Failure: timeout/error -> next provider; all fail -> abstain to humans; never fabricate. Budget: daily caps per call site from autonomy/POLICY.yaml; over cap -> local/rules. Security: TYPESAFE_API_KEY from secret manager on control-plane runners only; network allowlist adds TypeSafe host only there, never trading hosts or research sandboxes. Hard guard: platform/decide not importable from engine/, strategies/, or live services (import-lint CI + test).

## Phase 3 — call sites (control plane only)
swarm.route_task (Choice; agents/orchestrator; low confidence -> architect) · pr.tier_hint (Choice; autonomy-gate; ADVISORY ONLY, deterministic gate authoritative, disagreement adds tier-mismatch label) · learnings.triage (Choice x2: novel/duplicate/noise + target file; loop-knowledge; auto-archive high-confidence noise else PR) · ci.failure_class (Choice: flaky/infra/regression/dependency/test bug; loop-health) · backlog.duplicate (YesNo; loop-backlog; dedupe above threshold) · backlog.value (Score; loop-backlog; one ranking input only) · research.paper_relevance (Score; loop-discovery; pre-filter) · alert.triage (Choice: now/later/noise; ops/monitoring; NEVER auto-suppresses risk or kill-switch alerts) · injection.check (YesNo; every external-ingest loop; quarantine above low threshold; one layer of injection breaker).
Each: golden labeled set >=200 examples from real repo history in platform/decide/evals/<callsite>/, with labeling definition and labeler. Out of scope: orders, positions, sizing, risk limits, model promotion, strategy gate passage, fund reporting.

## Phase 4 — evals
Per call site head-to-head: Jev pinned, small-Claude fallback, rules, later local. Report accuracy, ECE + reliability diagram, selective accuracy vs coverage, latency p50/p95, cost per 1000, failure/timeout rate. Provider order + thresholds from data into config/decide.yaml with report linked. Vendor numbers don't count. Re-run monthly, on pin bump, on live disagreement drift. `just decide-evals` + weekly CI. Meta loop treats call-site configs like skills.

## Phase 5 — distillation (platform/decide/distill/)
Ledger decisions joined with outcomes (human override, revert, routed agent success, CI fix held). Dataset: scrubbed inputs + outcome-corrected label. Local model: embeddings + logistic regression or small fine-tuned encoder, calibrated. Replacement gate: local matches/beats Jev on eval, calibration no worse, safety call sites unaffected -> T2 PR moving local ahead. Track vendor share (down) and cost saved.

## Phase 6 — in-house signal model (ml/, engine/crates/inference/, docs/adr/0012-signal-model.md)
Ternary label per horizon up/flat/down over next N ms or N events; flat = move smaller than spread + fees + expected slippage; horizons per strategy; labels in code with tests. Baseline: regularized logistic regression, then GBT; features OFI, queue imbalance, microprice deviation, trade sign flow, short-horizon vol, spread state, cross-venue lead-lag. Calibration isotonic/Platt on held-out walk-forward fold; reliability per regime. Selective output: direction only above calibrated-confidence threshold chosen by max net EV after costs on validation; optional conformal sets. Validation: purged embargoed walk-forward, deflated Sharpe with true trials, PBO, coverage vs net-EV, regime/venue stability. Sequence models only if beating trees OOS after costs and within latency budget. Rust: ONNX or codegen from ml/export; hash-checked load vs registry; Python/Rust parity within tolerance; criterion latency bench; missing features / stale / NaN / OOD -> no signal; risk crate checks every order regardless. Hooks into loop-models: retrain challenger, calibration drift monitoring, auto-demote on decay, promotion via shadow + dossier + human.

## Phase 7 — docs
platform/decide/CLAUDE.md (sendable data classes, no trading call sites, how to add a call site: golden set, eval, config, data-class proof; providers pinned). Root CLAUDE.md: add platform/ to repo map; one line: "Jev and other hosted decision APIs: control plane only; never engine, strategies, or live services." engine/ + ml/ CLAUDE.md: selective-classifier contract. Skills add-decision-callsite, run-decide-evals, distill-callsite. Glossary: System One model, calibration, ECE, selective classification, abstention, coverage, conformal prediction, distillation. how-this-repo-learns.md decision layer section.

## Acceptance
ADRs 0010-0012 human-approved; importing platform.decide from engine/strategies/live fails CI; trading hosts + sandboxes can't reach TypeSafe host (network test); scrubber blocks seeded API key, strategy parameter file, PnL record; every call site >=200 golden + eval report, thresholds link to reports; failure/timeout -> fallback -> abstain, never fabricated; pr.tier_hint can't change gate verdict (test); alert.triage can't suppress risk/kill-switch alerts (test); ledger records provider, pinned version, latency, cost, scrubbed input hash; distillation trains local model for >=1 call site with comparison report; signal model trains, calibrates, exports, Rust parity + latency bench pass, no-signal fallback tested for NaN, stale, OOD; weekly report has decision-layer spend, vendor share, calibration drift.

## Do NOT
No Jev/hosted AI from engine, strategies, or any process with venue credentials. No Jev/LLM outputs on historical text as backtest features without a lookahead-leakage review ADR. No Secret/Alpha data to any external API. No unofficial reseller endpoints/keys. No thresholds from vendor benchmarks. No probabilistic classifier replacing a deterministic gate; advise or pre-filter only.
