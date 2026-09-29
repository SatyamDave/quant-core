# ADR-0040: Agentic trading decision (final trade call moves to an AI agent)

## Status

Accepted. Root [CLAUDE.md](../../CLAUDE.md) rule 1 is amended by this decision for the
trade-decision step only; see "Rule 1 amendment" below.

## Context

This decision moves the final trade decision from a purely deterministic rule to an AI agent: the
Rust engine keeps producing a calibrated signal, but an agent service makes the trade / no-trade /
side / size call, and places orders only through a risk-wrapped tool, which then talks to a venue
(SimVenue today, your broker adapter later). This changes root rule 1 ("no agent or LLM call in the
live trading path"), which [ADR-0010](0010-decision-layer.md) had used to keep every hosted decision
model out of the trading path. That reasoning does not disappear -- latency, determinism and
leakage are still real constraints -- but this ADR accepts those costs for the trade-decision step
specifically, with a deterministic risk engine and kill switch as the check that still runs
regardless of what the agent proposes.

- The engine's deterministic risk checks, kill switch, OMS and replay determinism (root rules 4, 7,
  11) are unchanged: nothing in this ADR touches `engine/crates/risk/**` or loosens a limit.
- This ADR does not pick a venue. It picks the shape of the decision pipeline that sits in front of
  whichever broker adapter is built.

## Decision

### 1. The pipeline

```
lab (offline) → Rust engine (order book, features, qc-inference classifier)
→ qc-bridge (Rust, exposes the engine's decisions over a bridge protocol)
→ agent service (TypeScript, in-process wrapper tool only)
→ submit_order_intent (the agent's only callable action)
→ risk (qc-risk + kill switch, runs regardless of who called submit_order_intent)
→ OMS → gateway → SimVenue (tests/paper) | your broker adapter (live)
```

The classifier (`qc-inference`) keeps producing a calibrated signal (direction, class
probabilities, model hash); it is trained and gated offline and makes no network call. The engine
assembles a `DecisionRequest` and hands it to `qc-bridge`, which exposes it to the agent service.
The agent's decision comes back as a proposed `Decision`, submitted as an `order_intent` through
`submit_order_intent`, and from there the deterministic risk engine, kill switch and OMS run exactly
as they do for any other order source. The load-bearing invariant: **risk is authoritative, not
advisory**; a check that could not run is not a check that passed.

### 2. Handoff protocol v1

Transport: the agent service spawns the `qc-bridge` binary (`engine/crates/bridge`) and exchanges
JSON Lines over its stdin/stdout. Every message carries `"v":1` and a caller-chosen `"id"` that is
echoed back. Money and quantity fields are decimal strings, never JSON floats; timestamps are
integer nanoseconds (`ts_ns`).

Schemas live at `schemas/decision/v1/*.schema.json` (JSON Schema 2020-12): `decision_request`,
`decision`, `order_intent`, `intent_result`, `bridge_request`, `bridge_response`,
`decision_ledger_entry`. Both sides test against the same schema files.

Operations: `next_decision_request` (the next `DecisionRequest`, or `null` when the feed is
exhausted), `submit_order_intent` (runs `RiskEngine` + kill switch + OMS and returns
`{accepted, client_order_id?, risk_reject?, halted?}`), `no_trade`, `status`, `shutdown`. An unknown
op, a bad version, or a schema violation returns a typed error, never a guessed default.

`DecisionRequest` carries: `request_id`, `ts_ns`, `instrument`, `best_bid`/`best_ask`/`mid`/
`spread_ticks`, `features`, `signal` (`{direction, probs, model_sha256}` or `null`), `position`,
`limits` (`max_position`, `max_notional`, `max_order_rate_per_sec`, `remaining_daily_loss`), and
`allowed_actions` (a subset of `["buy","sell","no_trade"]` -- the engine, not the agent, decides
what is offerable).

`Decision` (schema-validated structured output): `request_id`, `action`, `qty`, `limit_price`,
`confidence` (0-1, **advisory only**), `rationale` (≤ 500 chars).

### 3. Isolation: the agent never sees a broker tool

No broker MCP server or broker client is ever registered as an agent tool. The agent's model loop
registers exactly one in-process tool (`submit_order_intent`), with every built-in tool stripped,
an allow-list naming only that tool, unmatched calls denied outright, and a code-level pre-tool hook
as the final deny.

The broker gateway lives in `agent/src/broker/` as a separate module from the agent's reasoning
loop, is never registered as a tool, and only executes an order the bridge has already approved:
the bridge returns an approval token bound to the intent's hash, and the gateway reports acks/fills
back to the bridge's OMS. There is no code path by which the model's tool-calling loop can reach a
broker credential or a broker tool schema.

### 4. Approval token

`intent_result` on `accepted: true` includes a token bound to a hash of the normalized intent
(instrument, side, qty, limit_price, request_id). The gateway refuses any order whose token does not
verify against that hash, so a compromised or buggy process downstream of the bridge cannot alter an
order after risk approved it (the same principle as root rule 9).

### 5. Fake and live modes

`QC_AGENT_MODE=fake` (the default, and the only mode wired into CI) uses a deterministic rule-based
decider with no network call and no API key, so `just agent-sim` and CI run the whole loop end to
end. Live mode uses a model provider and requires its API key plus `scripts/ci/ai_gate.py` allowing
the `autonomy/POLICY.yaml` entry `loop-agent-eval`, which ships **disabled, at zero budget**.
`just agent-sim` runs the loop twice on `tests/replay/sample_day.csv`; identical ledger hashes are
required (root rule 7, extended to the fake-mode agent loop).

### 6. Decision ledger

Every `DecisionRequest`, prompt version, model id, full request/response (or a `"fake"` marker),
`Decision`, risk verdict and `IntentResult` is appended to `out/agent/ledger.jsonl` (gitignored,
append-only, schema `decision_ledger_entry`) -- root rule 6 applied to agent decisions.

### 7. Learning loop

- **Predictive learning**: the classifier retrains offline (ADR-0013), promoted only through the
  gates plus a human. Agent decisions are never used as classifier training labels.
- **Agent learning**: prompt, tool and routing changes are challengers, evaluated against the
  champion on the frozen scenario suite and on shadow decisions, promoted only when both improve;
  failures go to the graveyard.
- **Research memory**: ledger outcomes become labelled scenarios and research questions.
- **Authority stays bounded**: the agent can change what it decides; it can never change the risk
  limits, the kill switch, or account funding (root rule 3).

### 8. Eval tiers

1. **Every PR**: deterministic recorded-response replay (`just eval`) with zero model calls,
   including the permission test proving no tool but `submit_order_intent` is callable.
2. **Before any live-capital change**: a frozen scenario suite with a fixed baseline (buy-and-hold /
   no-trade / classifier-only) as the bar to beat, not zero.
3. **Ongoing, budget-capped**: prospective shadow-mode logging until a pre-planned sample size
   supports a claim, with multiple-comparison correction.

Live-model evals (tiers 2 and 3) are gated by `scripts/ci/ai_gate.py` and `loop-agent-eval`.

### 9. Rule 1 amendment

> **Rule 1 (amended by ADR-0040).** No agent or LLM call inside `engine/`, and no process that holds
> venue credentials ever makes one either. The one exception is the trade-decision step: a
> risk-wrapped agent service may propose a trade through `submit_order_intent`, but it never holds a
> broker tool or credential, and every proposal is independently checked by the deterministic risk
> engine and kill switch before anything reaches a venue. Changing this further needs a new ADR, not
> a CLAUDE.md edit.

## Alternatives

- **Keep the LLM out of the order path entirely (ADR-0010).** The safer default. Not chosen because
  the point is to test, forward-only and honestly measured, whether an agent's judgment adds value
  over the fixed rule.
- **A plain model SDK with a manual tool-use loop instead of an agent framework.** Smaller trusted
  surface; the documented fallback if the isolation pattern in §3 proves insufficient.
- **A broker adapter with no agent in front of it.** Still valid and lower risk; the bridge surface
  has no agent-specific assumption, so this remains available.

## Consequences

- `engine/crates/bridge/` and `agent/` are call sites with their own lifecycle, T2 by default under
  `autonomy/POLICY.yaml`; `loop-agent-eval` ships disabled at zero budget.
- The decision ledger is Internal data (`docs/security/data-classes.md`) and is not committed.
- Removing the agent step is a configuration change, not a rewrite: risk and OMS do not know or care
  where an intent came from.

## Known limits of an agentic decision step

1. **Latency.** A hosted-model decision takes roughly 1-5 seconds single-turn and more multi-turn.
   This is not high-frequency trading; measure the real loop (`just bench-latency`) rather than
   assuming a frequency.
2. **The agent cannot be honestly backtested on history.** A model's pretraining may already
   describe the period, so a historical "prediction" can score recall, not skill. The classifier is
   backtested (walk-forward); the agent is evaluated forward-only (shadow, then canary).
3. **Eval cadence splits by cost and determinism.** Deterministic replay on every PR; live-model
   evals on merge or schedule, budget-gated.
4. **Parallel agents need equal-token-budget evidence.** Multi-agent gains in the literature are
   often uncontrolled extra compute, and same-family models have correlated errors.
5. **The broker is not your limit.** Many broker agent integrations have no configurable spending
   cap and revocation may not be retroactive. The risk-wrapped gateway enforces per-order and
   per-day caps, order-rate caps and the kill switch.
6. **Market-conduct liability is the operator's.** The gateway includes self-cross prevention rather
   than leaving it to the agent's judgment.
7. **Provider and broker terms.** Check your model provider's usage policy (financial decisions,
   training on outputs) and your broker's API and automation terms before trading real money.

## Review date

Before a live broker adapter is enabled, or if a model provider's usage policy or a broker's API
terms change.
