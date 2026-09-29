# Agent interface: MCP server and local control API

quant-core gives your own agent (Claude Code, or any MCP client) one small surface for managing
trades. The agent can read status and rules, tighten a rule, read its decisions, submit an order
intent, and pull the kill switch. Both surfaces below call the same control core
(`agent/src/control/index.ts`). Every order goes through qc-bridge's risk-wrapped
`submit_order_intent`, and the engine's risk checks and kill switch stay authoritative.

## What no surface can do

- **Loosen a limit.** `propose_rule_change` applies a tightening straight to
  `config/limits/*.toml`, which takes effect when the bridge next restarts. It refuses any
  loosening, unknown key or malformed value. Loosening needs a human edit with two approvals
  (root CLAUDE.md rule 4).
- **Release the kill switch.** `engage_kill_switch` is one-way. To release it, a human deletes
  the kill file (`$QC_KILL_FILE`, default `ops/live/state/KILL`) and restarts the bridge.
- **Reach a real venue by default.** When the bridge runs in external venue mode, order intents
  are refused unless the operator started the server with `QC_CONTROL_ALLOW_EXTERNAL=1`. The
  agent cannot pass that as an argument. Even then the result is only a risk approval, and your
  broker adapter (`agent/src/broker/README.md`) executes it.

## Tools / endpoints

| MCP tool | HTTP (127.0.0.1:7070) | Arguments |
|---|---|---|
| `get_status` | `GET /api/status` | none |
| `get_rules` | `GET /api/rules` | none |
| `propose_rule_change` | `POST /api/rules` | `{ "key": "max_notional", "value": "10" }` |
| `submit_order_intent` | `POST /api/orders` | `{ "intent": <order_intent.schema.json> }` |
| `list_decisions` | `GET /api/decisions?limit=20` | `limit` (1..1000) |
| `list_orders` | `GET /api/orders?limit=20` | `limit` (1..1000) |
| `engage_kill_switch` | `POST /api/kill-switch` | `{ "reason": "..." }` (optional) |

The intent is validated against `schemas/decision/v1/order_intent.schema.json` before it reaches
the bridge. Over MCP, a refusal comes back as a tool result with `isError: true`, so the model
reads the reason. Over HTTP, a refused loosening returns 403 and any other bad input returns 400.

## MCP server (`just mcp`)

Stdio, newline-delimited JSON-RPC 2.0 (`initialize`, `ping`, `tools/list`, `tools/call`). Put
this in `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "quant-core": {
      "command": "just",
      "args": ["mcp"],
      "env": {
        "QC_BRIDGE_BIN": "../engine/target/release/qc-bridge",
        "QC_BRIDGE_ARGS": "../tests/replay/sample_day.csv --limits ../tests/replay/limits.toml --model <model> --model-sha256 <sha>"
      }
    }
  }
}
```

Paths are relative to `agent/`, where `just mcp` runs. The `just agent-sim` recipe in the
justfile shows the full replay argument list, including how it finds the model and its sha256.
Without `QC_BRIDGE_BIN`, the read-only tools and the kill switch still work, but
`submit_order_intent` reports that no bridge is attached. The bridge starts on first use. If
`QC_BRIDGE_ARGS` includes `--follow`, status reports the mode as `paper`.

## Local control API (`just control-api`)

This is a `node:http` server that binds 127.0.0.1 only, on port `QC_CONTROL_PORT` (default 7070).
It serves the operations above under `/api/*` and the UI's static files (`ui/dist`, else `ui/`)
at `/`. The page in your browser is the only other origin that can reach it, so:

- requests whose `Host` is not `127.0.0.1` or `localhost` get 403, which blocks DNS rebinding;
- a POST must be `content-type: application/json`, which cross-site forms cannot send without a
  CORS preflight, and the server never answers a preflight.

There is no authentication. Any local process running as your user can call it, just as it could
edit the limits file itself.

## Tests

`cd agent && npx vitest run tests/control.test.ts tests/control-surfaces.test.ts`
