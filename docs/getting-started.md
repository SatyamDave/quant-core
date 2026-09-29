# Getting started

This walks you from a fresh clone to an agent placing simulated trades through the risk gate.
Nothing here needs broker credentials or a paid API key.

> quant-core is not financial advice and comes with no warranty. See the [README](../README.md).

## 1. Install

You need Rust (stable), [uv](https://docs.astral.sh/uv/), [just](https://just.systems/),
Node.js 20+ and git.

```sh
git clone https://github.com/OWNER/quant-core.git && cd quant-core
just setup         # Rust crates + research Python environment
just agent-setup   # agent service npm packages
just check         # everything CI runs; should pass on a clean clone
```

`just` with no arguments lists every recipe.

## 2. See the engine is deterministic

```sh
just replay
```

This replays a synthetic market day through the engine twice and prints two identical hashes
that match `tests/replay/expected_order_log.sha256`. Determinism is what makes every decision
auditable.

## 3. Run the agent loop in simulation

```sh
just agent-sim
```

The agent service, using the offline fake decider, pulls decision requests from the real
`qc-bridge`, answers `submit_order_intent` or `no_trade`, and the engine's risk gate approves or
rejects each intent before SimVenue fills it. The loop runs twice and must produce the same
ledger hash. Read the decisions with `just report-ledger`.

## 4. Open the UI

```sh
just control-api
```

Open <http://127.0.0.1:7070> (`QC_CONTROL_PORT` changes the port). You see engine status, your current limits, the decision
ledger and the kill switch.

## 5. Connect your own agent (MCP)

```sh
just mcp
```

Add it to your agent client's MCP config (for example `.mcp.json`):

```json
{ "mcpServers": { "quant-core": { "command": "just", "args": ["mcp"] } } }
```

Your agent can read status and the ledger, tighten a limit, submit an order intent (which the
risk gate still checks) and engage the kill switch. It cannot loosen limits or reach a broker.
The full tool list is in [agent-interface.md](agent-interface.md).

## 6. Set your rules

Limits live in `config/limits/default.toml`, or `config/limits/<instrument>.toml` for one
instrument: max position, max notional per order, order rate, max daily loss, price band, stale
data and max daily notional. Keep them small. Your agent can only tighten them at runtime;
loosening them is a deliberate human edit.

## 7. Try a real decider

Set `QC_AGENT_MODE` to pick the decider: fake (default), `replay`, `live` (Claude Agent SDK, needs
`ANTHROPIC_API_KEY`) or `openrouter`. Keys go in your shell or a gitignored `.env`, never in git.
Run `just eval` to check a decider against the frozen suite before trusting it.

## 8. Go further

- Write a strategy: [strategies/writing-a-strategy.md](strategies/writing-a-strategy.md)
- Connect a broker: [brokers/writing-a-broker-adapter.md](brokers/writing-a-broker-adapter.md)
- How it fits together: [ARCHITECTURE.md](ARCHITECTURE.md)
- If something goes wrong: [runbooks/kill-switch.md](runbooks/kill-switch.md)

Stay in simulation and paper until you understand every limit and every runbook. A paused bot
costs nothing.
