# ADR-0010: Decision layer (hosted decision models in the control plane only)

## Status

Proposed. Needs human approval before any Phase 2 code is written. Partly superseded for trade
decisions by [ADR-0040](0040-agentic-decision.md) (Accepted): a risk-wrapped agent may now propose the final trade decision. Decision point 1 below
("Jev and every other hosted decision API are not used in the trading engine... or for price
prediction") still holds for the deterministic engine and for Jev specifically; it no longer holds
for every hosted decision model everywhere, since ADR-0040 puts a different one (a Claude Agent SDK
service) after the classifier and before risk. The control-plane use (Decision point 2) is
unaffected.

## Context

TypeSafe AI publishes Jev, a hosted "System One" model. It takes a `state` (text, a JSON object, or an array of text) and a map of typed questions, and returns typed answers with probabilities. The three question types are Choice (pick one option from a set), Score (rate against ordered levels) and Noul (probability that a yes/no statement is true). Choice and Score answers also carry a `confidence` value; a Noul answer carries only its probability. Sources and the full vendor review are in [ADR-0011](0011-typesafe-vendor-review.md).

This repository has two very different places where a fast typed decision could be used:

- The trading path: the Rust engine, strategies and every live service. Root rule 1 forbids any network call to an AI API from `engine/` or from any process that holds venue credentials. Root rule 7 requires that replaying a recorded market day reproduces identical orders.
- The control plane: the offline agent swarm, CI, the autonomy loops, backlog and research triage, and monitoring summaries. These processes hold no venue credentials and produce PRs, reports and labels.

A hosted decision model is a poor fit for the trading path for four independent reasons:

1. Latency. A network round trip to a hosted API is orders of magnitude slower than the engine's tick-to-trade budget. TypeSafe publishes no latency commitment (see ADR-0011), and the Python SDK's default timeout is 10 seconds.
2. Determinism. The vendor can change the service, and aliases such as `jev-latest` move to new model versions. A replay that depends on a remote model cannot be guaranteed to reproduce the same orders, which breaks root rule 7.
3. Credentials. Any such call from a process with venue credentials breaks root rule 1.
4. Data fit and leakage. Jev is not trained on our order book data, and a model pretrained on historical text can carry knowledge of events after a backtest's timestamp. Using its outputs as backtest features risks lookahead leakage.

The control plane has the opposite profile. Its decisions (which agent takes a task, whether a CI failure is flaky, whether two backlog items are duplicates) tolerate hundreds of milliseconds or seconds, are advisory or reversible, and already pass through PR review.

## Decision

1. **Jev and every other hosted decision API are not used in the trading engine, in strategies, in any live service, or for price prediction.** This follows from root rules 1 and 7 and from the latency and leakage reasons above.
2. **Jev may be used in the control plane only**, for these kinds of decisions: swarm task routing; triage of learnings, CI failures and alerts; backlog deduplication and scoring; research-paper relevance filtering; injection screening of external content; and an advisory second opinion on autonomy tiers. The call sites are listed in the decision-layer spec (`docs/specs/0003-decision-layer.md`). A decision-layer output never replaces a deterministic gate. It may advise, pre-filter or escalate, and it may never suppress a risk or kill-switch alert.
3. **The trading model follows the same idea, built in-house.** The engine's signal model is a calibrated selective classifier trained on our own data and exported to deterministic Rust inference. It outputs probabilities and returns "no signal" when its confidence is below a calibrated threshold. That model gets its own ADR (0012, Phase 6).
4. **Every hosted decision call goes behind a provider interface.** Each call site names an ordered list of providers (deterministic rules, a local model, Jev, a small Claude model) with thresholds taken from evaluation data. Every decision is logged with its provider and pinned model version, and is evaluated against the alternatives. No core function depends on a single vendor: if every provider fails, the call site abstains and hands the item to a human. It never fabricates an answer.
5. **Local replacement models are trained only in ways the vendor contract allows.** The TypeSafe Master Customer Agreement, section 2.3(b), forbids using the Services or any Output "to perform model distillation, train a model to imitate the output of the Services". Until a human with legal authority reviews this, local models for a call site are trained on our own outcome labels (human overrides, reverts, whether a routed agent succeeded, whether a CI fix held) and not on Jev's answers or probabilities. See ADR-0011 for the clause and the open question.
6. **Only Public and Internal data may be sent**, after the scrubber, as defined in [docs/security/data-classes.md](../security/data-classes.md).

## Alternatives

- **Use Jev in the trading path for fast classification.** Rejected for the four reasons in Context. Each alone is disqualifying.
- **Do not use a hosted decision model anywhere.** This is the safe default and remains the fallback: every call site must work with rules plus human review. It was not chosen as the policy because control-plane triage is repetitive, low-stakes and reviewable, and a measured comparison (Phase 4) is cheaper than guessing.
- **Use a general LLM with structured output for every call site.** Kept as one provider behind the same interface, not the only one. The comparison between providers is made on our own golden sets in Phase 4.
- **Call the vendor SDK directly from each call site.** Rejected. It would scatter pinning, scrubbing, budgets, logging and fallback across many files and make vendor removal a multi-file change.

## Consequences

- `platform/decide/` (Phase 2) must be impossible to import from `engine/`, `strategies/` or live services, enforced by an import-lint check in CI and a test. The TypeSafe host is added to the network allowlist of control-plane runners only.
- Each call site needs a golden labeled set of at least 200 real examples and an evaluation report before it ships. Thresholds come from those reports, never from vendor benchmarks.
- The decision ledger stores scrubbed inputs and outputs. That ledger is itself Internal data and follows the same data rules.
- Removing TypeSafe is a configuration change: drop it from each call site's provider order. Call sites keep working on rules, local models or human review.
- Point 5 may limit Phase 5 (distillation) until the legal question in ADR-0011 is answered. The rest of the layer does not depend on it.

## Review date

2026-12-27, or earlier if TypeSafe changes its terms, if ADR-0011 questions are answered, or before Phase 5 starts.
