# Replay fixtures (issue #36)

Three recorded decisions for the deterministic replay tier (ADR-0040 Decision §8, tier 1 —
Chronicle-style cut-point replay, arXiv:2609.20625): a clean buy, a clean `no_trade`, and one the
deterministic risk engine correctly rejects downstream.

**These are synthetic.** `agent/src/decider/live.ts` has never made a real Anthropic API call in
this repo (root hard limit: no real Anthropic call, no API key, no live-mode run yet). Every file here is hand-authored to be schema-valid and
plausible, not captured from a genuine model response. Each fixture says so in two places so it
cannot be mistaken for a real recording:
- top-level `"model": "synthetic-fixture-do-not-treat-as-live"` (never a real model id),
- `"raw".synthetic: true` plus a `"raw".note` explaining the same thing.

Each fixture is a full `DecisionLedgerEntry` (validated against
`schemas/decision/v1/decision_ledger_entry.schema.json` on load — `loadReplayFixtures` in
`../../src/decider/replay.ts` throws on load if one stops matching the schema):

| File | `request.request_id` | `decision.action` | `result` |
|---|---|---|---|
| `synthetic-clean-buy.json` | `fixture-clean-buy-001` | `buy`, sized within the request's own limits | `{accepted: true, client_order_id: 1}` |
| `synthetic-clean-no-trade.json` | `fixture-clean-no-trade-001` | `no_trade` (flat signal) | `null` (no order was ever proposed) |
| `synthetic-risk-rejected.json` | `fixture-risk-rejected-001` | `sell`, qty 50 — exceeds the request's `max_position` of 10 | `{accepted: false, risk_reject: "qty 50 exceeds max_position 10"}` |

The third fixture exists to document the load-bearing invariant ADR-0040 states directly: a
`Decision`'s `qty`/`confidence` are advisory, never a risk clamp (`confidence` is explicitly
"advisory only" per `decider/fake.ts` and `schemas/decision/v1/decision.schema.json`), so a
decider — fake or live — can propose something the deterministic risk engine still refuses. This
fixture's `result.accepted: false` records that outcome; replaying it (see below) only replays the
*decision* (what the decider proposed), not a re-run of the risk engine itself, which lives in
`engine/crates/risk/` and is out of this lane's scope.

## What replays and what doesn't

`ReplayDecider` (`../../src/decider/replay.ts`) reads every `*.json` file in this directory,
indexes them by `request.request_id`, and — given a `DecisionRequest` whose `request_id` matches
one on file, and whose full contents deep-equal the recorded `request` — returns exactly the
recorded `decision`, `raw`, and `cost_usd`, every time, with zero network calls and zero calls
into `@anthropic-ai/claude-agent-sdk`. A `request_id` with no matching fixture, or one whose
contents no longer match what was recorded, throws rather than guessing (root CLAUDE.md rule 12:
fail closed). See `agent/tests/decider-replay.test.ts` for the replay tests, including a
deliberately-broken-then-reverted change proving the tests actually catch a regression.

## Recording procedure once live mode is enabled (#65)

These synthetic fixtures are placeholders for genuine recordings. Once `QC_AGENT_MODE=live` runs
for real (gated by `ANTHROPIC_API_KEY` and `autonomy/POLICY.yaml`'s `loop-agent-eval`, per
`agent/README.md`), the recording procedure is:

1. Run the live decider against `tests/replay/sample_day.csv` (or another frozen scenario) with
   `QC_AGENT_MODE=live`. `loop.ts` already appends one `DecisionLedgerEntry` per decision to
   `out/agent/ledger.jsonl`, with `mode: "live"` and the full SDK request/response under `raw`.
2. Pick at least one clean buy/sell, one clean `no_trade`, and one the risk engine rejects, from
   that ledger.
3. Copy each chosen line into its own file here (drop the surrounding `out/agent/ledger.jsonl`
   framing; each fixture is one `DecisionLedgerEntry` object, not a JSONL line), remove the
   `"synthetic"`/`"note"` markers this generation added (they no longer apply to a real
   recording), and update this table.
4. Never edit a committed fixture's `decision` or `raw` by hand afterward — a fixture is a
   recording, not a place to touch up an inconvenient result; if the model's real behavior
   changes, record a new one instead.
