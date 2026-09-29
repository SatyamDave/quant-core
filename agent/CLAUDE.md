# agent/ — TypeScript agent service: makes the final trade decision through qc-bridge (ADR-0040)

## Owns / does not own
- Owns: the Claude Agent SDK harness (`src/`), `prompts/` (versioned), the decider that turns a `DecisionRequest` into a `Decision`, the append-only decision ledger writer, `src/broker/` (the generic `BrokerAdapter` interface and the approval-verifying gateway — never registered as an agent tool; it only executes an intent `qc-bridge` already approved; see `src/broker/README.md` to write an adapter), `src/recorder/` (issue #46: polls the adapter's read-only quote/book methods -- `pollBook()` gets real Level 2 depth where the asset class has one, falling back to a flagged top-of-book quote otherwise or when a book side comes back empty -- and writes `data/raw/`+`data/catalog/`; a separate concern from `src/broker/`'s order-execution role, handed only the read-only half of the adapter, not part of the decider/bridge path at all).
- Does not own: whether an intent is accepted (qc-risk, qc-bridge), order state (qc-oms), the classifier signal (qc-inference), limits and the kill switch (config/limits/, qc-risk) — the agent proposes, `qc-bridge` disposes.

## Commands
- `just agent-setup`, `just agent-test`, `just agent-sim` (fake mode against tests/replay/sample_day.csv; running it twice must give identical ledger sha256)
- `QC_AGENT_MODE=fake ...`: no network, no key, what CI runs. `QC_AGENT_MODE=live ...`: needs `ANTHROPIC_API_KEY` and `scripts/ci/ai_gate.py loop-agent-eval` to allow it.
- `QC_AGENT_MODE=openrouter ...` (ADR-0042): one JSON-only call to a free OpenRouter model, no tools; needs `OPENROUTER_API_KEY`, `QC_OPENROUTER_MODEL` and `ai_gate.py loop-agent-openrouter` to allow it. At the default zero spend cap, `QC_OPENROUTER_MODEL` must end `:free` or be listed at `pricing.prompt`/`pricing.completion`/every other price field exactly `"0"` on `GET https://openrouter.ai/api/v1/models` when the process starts (`resolveOpenRouterConfig`, `src/decider/openrouter.ts`) — a failed fetch, missing model, or nonzero price refuses to start. Every bad reply becomes `no_trade`; a positive cost past `QC_OPENROUTER_MAX_USD_PER_DAY` (default 0) stops the loop.
- `just ensemble-compare` (issue #54): single decider vs an N-member ensemble at the exact same total AI usage, over the frozen suite; reports P&L, member agreement, and an ADOPT/DO_NOT_ADOPT verdict. Never wired into the live loop by default — `QC_ENSEMBLE_SIZE`/`QC_ENSEMBLE_AGGREGATION` opt `cli.ts` in.

## MUST
- Exactly two SDK tools are registered, both from one in-process `createSdkMcpServer()`/`tool()` wrapper over `qc-bridge`: `submit_order_intent` and `no_trade`. `tools: []`; `allowedTools` names only these two; `permissionMode: "dontAsk"`.
- A `PreToolUse` hook denies any tool name other than the two above — defense in depth in case a future tool is added without updating the allowlist; `canUseTool` alone is not enough.
- `@anthropic-ai/claude-agent-sdk` is exact-pinned in `package.json` (no `^`/`~`). `tests/permission-e2e.test.ts` proves the real permission-decision code path denies everything but the two wrapper tools by driving `buildOptions()` through a hand-reverse-engineered stub of the SDK's stream-json control protocol (`tests/helpers/fakeClaudeCli.mjs`), not a documented API — a version bump can silently change that protocol out from under the stub. **Any SDK version bump must re-run `npm test` (this suite included) before merging**, and if the stub's assumptions about `control_request`/`hook_callback`/`mcp_message` shapes stop matching, update `fakeClaudeCli.mjs` in the same PR as the bump, not after.
- Both tools only forward to the bridge's `submit_order_intent` / `no_trade` ops; neither talks to a venue or broker directly. The loop fetches `next_decision_request` itself; the model never pulls market data through a tool.
- Every prompt has a version string; every ledger entry records that version plus the model id. `QC_PROMPT_VERSION` (live mode only) overrides which `prompts/decide-<v>.md` is used, validated against the files actually on disk — an unrecognized version refuses to start rather than falling back (issue #54, for `scripts/eval/promote.py` prompt A/B tests).
- `QC_ENSEMBLE_SIZE`/`QC_ENSEMBLE_AGGREGATION` (issue #54) wrap the chosen mode's decider in an N-member panel (`src/decider/ensemble.ts`); live members split one total per-decision budget equally, never more. Unset (default): today's single-decider behavior, byte-for-byte.
- Every `DecisionRequest`, prompt version, model id, full SDK request/response (or `"fake"` marker), `Decision`, and `IntentResult` is appended to `out/agent/ledger.jsonl` (gitignored, append-only; root rule 6).
- Live mode needs `ANTHROPIC_API_KEY` (secret manager, never in git — root rule 2) and a true result from `scripts/ci/ai_gate.py loop-agent-eval`; fake mode needs neither.
- `src/broker/` executes only an order `qc-bridge` already approved (an approval token bound to the intent hash); it is never one of the two SDK tools above.
- `src/recorder/` reaches a broker only through `src/recorder/allowlist.ts`'s `readOnly()` wrapper, which exposes only `getQuote`/`getBook`; `agent/tests/recorder/allowlist.test.ts` proves no place/cancel/preview method is reachable through it.
- `src/broker/heartbeat.ts`'s `Heartbeat` runs on its own `setInterval` (default 1s, CLAUDE.md rule 11's budget), independent of `runLoop`'s own await chain — issue #44 reopened: a hung decider (or a stuck feed) must not stop the bridge's `status`/kill-file from ever being observed again. `createExternalModeGateway` builds it but does not start it; `cli.ts` calls `heartbeat.start()`/`stop()` around the loop. Once a tick sees `halted`, it pulls `status.pending_cancels` (protocol v1.2.1) and calls `BrokerGateway.cancelOrder` for each — safe to call every tick until nothing is left open, since the bridge mints a fresh, unexpired approval on every call rather than storing one.

## NEVER
- Never register a broker's API or MCP server, or any tool beyond the two above, as an agent tool. Check: `grep -rn 'allowedTools' src` lists only `submit_order_intent` and `no_trade`.
- Never let the agent hold, read, or forward a broker credential; the broker adapter (`QC_BROKER_MODULE`) loads its own credentials from a secret manager and never passes them to the decider or a prompt.
- Never edit or skip a ledger line; corrections are new entries.
- Never train `ml/`'s classifier on this agent's decisions or outputs as labels (ADR-0040; Anthropic Commercial Terms and Usage Policy bar training on Claude outputs).
- Never call this service from `engine/`, and never let `engine/` make a network or AI call itself (root rule 1); the only channel between them is `qc-bridge`'s stdin/stdout.
- Never let `src/recorder/` call a place/cancel/preview method, or hold anything beyond the adapter's read-only half; it never touches `qc-bridge`, the OMS, or the decision ledger (issue #46: "holds no credential that could place a trade").

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- schemas/decision/v1/*.schema.json (the handoff contract); engine/crates/risk/CLAUDE.md; engine/crates/oms/CLAUDE.md
- ADR-0040 (agent handoff): docs/adr/0040-agentic-decision.md
- engine/crates/bridge/CLAUDE.md (the watcher thread and `pending_cancels`/`drain_halt_cancels` this file's heartbeat consumes); docs/runbooks/kill-switch.md (measured end-to-end latency)
