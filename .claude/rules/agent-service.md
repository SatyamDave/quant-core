---
paths:
  - "agent/**"
---

# Agent service (ADR-0040)

- Exactly two SDK tools, both from one in-process `createSdkMcpServer()`/`tool()` wrapper over `qc-bridge`: `submit_order_intent` and `no_trade`. `tools: []`, `allowedTools` names only these two, `permissionMode: "dontAsk"`.
- A `PreToolUse` hook denies any tool name other than the two above; `canUseTool` alone is not enough.
- Never register a broker MCP server, or any other MCP server, as an agent tool. `src/broker/` calls it directly, outside the SDK's tool loop, and only to execute an order `qc-bridge` already approved (an approval token bound to the intent hash).
- `QC_AGENT_MODE=fake` (what CI runs): a deterministic rule-based decider, no network, no API key.
- `QC_AGENT_MODE=live`: requires `ANTHROPIC_API_KEY` from the secret manager (never in git — root rule 2) and a true result from `scripts/ci/ai_gate.py loop-agent-eval` (disabled, zero budget, until a human enables it in `autonomy/POLICY.yaml`).
- Prompts are versioned (a version string in the prompt file, recorded on every ledger entry).
- Every decision cycle appends to `out/agent/ledger.jsonl` (gitignored); the ledger is append-only, never edited or replayed by mutation.
- Never call `engine/` from here or vice versa except through `qc-bridge`'s stdin/stdout (root rule 1); the agent never holds a broker credential.
- Never use this agent's decisions or outputs as training labels for `ml/`'s classifier (Anthropic Commercial Terms and Usage Policy bar training on Claude outputs).
