# ADR-0042: Decide with a free OpenRouter model instead of the paid Claude decider

## Status

Accepted. Amends [ADR-0040](0040-agentic-decision.md)
§5 (fake and live modes) for the model that makes the decision; everything else in ADR-0040 stands.

## Context

ADR-0040 made the Claude Agent SDK decider (`QC_AGENT_MODE=live`, `agent/src/decider/live.ts`)
the path to a real agent decision. For a small account, AI spend on the decision step has to be
close to $0: a paid call on every decision can spend a meaningful share of the account before
the first trade.

OpenRouter serves an OpenAI-compatible chat completions API
(`https://openrouter.ai/api/v1/chat/completions`) and lists models priced at zero, whose ids end
in `:free` (https://openrouter.ai/models?max_price=0). Every response carries a `usage.cost`
field with the amount charged (https://openrouter.ai/docs/use-cases/usage-accounting, checked
2026-09-28).

## Decision

1. Add `QC_AGENT_MODE=openrouter` (`agent/src/decider/openrouter.ts`). It makes one chat
   completions call with global `fetch` (no new dependency), using the same `prompts/decide-<v>.md`
   prompt plus a short "reply with only the Decision JSON" suffix, and `response_format` JSON.
   It registers no tools: the model returns a Decision object and nothing else.
2. The reply is parsed and validated against `schemas/decision/v1/decision.schema.json`, then
   checked against the request: `request_id` must match, the action must be in `allowed_actions`,
   and a buy or sell needs a positive `qty` and `limit_price` that keep the position within
   `max_position` and the order within `max_notional`. Anything that fails (HTTP error, 429,
   timeout, non-JSON, schema-invalid, outside the limits) becomes `no_trade`, with the reason in
   the ledger's `raw` field. Nothing is retried. qc-risk still re-checks every accepted proposal.
3. Spend: `cost_usd` is what OpenRouter reports, `"0"` only when it reports zero. When it reports
   nothing, `raw.cost_usd` is `"unknown"` (the ledger schema's `cost_usd` only takes a decimal) and
   the run continues only for a model confirmed priced at zero. A positive cost that takes the
   day's total past `min(QC_OPENROUTER_MAX_USD_PER_DAY, POLICY daily_usd)` stops the loop. The env
   cap defaults to `0`; while it is `0`, a model id ending `:free` is trusted on its id, and any
   other id is refused at start unless `resolveOpenRouterConfig` fetches the public
   `GET https://openrouter.ai/api/v1/models` and finds that id listed with `pricing.prompt`,
   `pricing.completion`, and every other pricing field exactly `"0"` (for example a
   zero-priced model id without the `:free` suffix). A
   failed fetch, a missing model, or any nonzero field all refuse the same as a paid id -- this
   check never widens what a `:free` id already gets on its own.
4. Gate: `ai_gate.py loop-agent-openrouter`, a new `autonomy/POLICY.yaml` entry, disabled with
   zero numbers. It is separate from `loop-agent-eval` so enabling the free model never enables
   the paid Claude decider. Required env: `OPENROUTER_API_KEY`, `QC_OPENROUTER_MODEL` (no default:
   free model ids change), optional `QC_OPENROUTER_TIMEOUT_MS` (default 20000).
5. The ledger records `mode: "live"` (the schema enum has no `openrouter` value) and
   `model: "openrouter:<model id>"`.

The Claude decider stays in the tree unchanged and remains the documented upgrade path once the
budget allows it.

## Consequences and risks

- Weaker models. Free models are smaller and less reliable than Claude at following a JSON-only
  instruction and at reasoning about spread versus edge. Most failures degrade to `no_trade`,
  which is safe but means fewer decisions reach the risk engine. A wrong but valid decision still
  goes to qc-risk, which is the check that holds either way. Model quality is not measured yet.
- Rate limits. OpenRouter documents 20 requests per minute and 50 requests per day for `:free`
  models on an account that has bought less than $10 of credits, and 1000 per day after $10
  (https://openrouter.ai/docs/api-reference/limits, checked 2026-09-28). A 429 becomes `no_trade`,
  so running out is safe but silent in effect. The decide cadence (`qc-bridge --decide-every N`,
  counted in book events) must be set so a trading day stays under the daily limit.
- Provider logging. Free-model providers may log or train on prompts. Our prompt holds only
  market data, the position and the limits: no account number, no key, no credential. A test
  (`agent/tests/decider-openrouter.test.ts`) checks the request body contains no env var value and
  no account number.
- The daily spend total lives in process memory and resets on restart. With the default cap of 0
  that does not matter; a positive cap would need the total read back from the ledger.

## Enabling it (operator steps)

1. Create an OpenRouter key and store it as `OPENROUTER_API_KEY` in the host's secret manager.
2. Pick a model from https://openrouter.ai/models?max_price=0 and set `QC_OPENROUTER_MODEL` to its
   id. Most zero-priced ids end in `:free`; an id that does not
   still starts, as long as OpenRouter's models list still prices it at zero when the process
   starts (`QC_OPENROUTER_MAX_USD_PER_DAY` above `0` skips this check entirely).
3. In `autonomy/POLICY.yaml` (T3), set `"enabled": true` at the top and change
   `loop-agent-openrouter` to `{"enabled": true, "daily_usd": 0.01, "max_turns": 1,
   "timeout_minutes": 1, "open_pr_cap": 0}`, and set `AUTONOMY_ENABLED=true` in the environment.
4. Run with `QC_AGENT_MODE=openrouter` and a `--decide-every` that keeps decisions per day under
   the free-tier limit above.
