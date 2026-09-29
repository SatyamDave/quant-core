# quant-core

**An open-source template for agentic trading: your agent's computer for managing trades.**

You set the rules: risk limits, instruments, trading hours and a kill switch. Your AI agent reads
market state and proposes trades. A deterministic Rust engine checks every proposal against your
rules before anything reaches a venue. Everything runs in simulation or paper mode by default.

> [!WARNING]
> **Not financial advice. Use at your own risk.** quant-core is research and engineering
> software, provided "AS IS" under the [Apache-2.0 license](LICENSE), with no warranty. Nothing
> in this repository is a recommendation to buy or sell anything, and nothing here is evidence
> of a profitable strategy. Automated trading can lose all of the money in an account, and
> faster than you can react. You alone are responsible for any account you connect, any order
> it places, and your compliance with your broker's terms and the law where you live. Start in
> simulation, stay in paper mode until you understand every limit, and never give an agent more
> capital than you can afford to lose.

## What it is

- **Deterministic engine** (`engine/`, Rust): order book, OMS, risk checks, kill switch, a
  simulated venue (SimVenue) and replay. It makes no network call and no AI call.
- **Risk gate** (`engine/crates/risk`, `config/limits/`): every order intent is checked against
  position, notional, order-rate, daily-loss, price-band and stale-data limits. Limits can be
  tightened by an agent at runtime, never loosened.
- **Agent service** (`agent/`, TypeScript): pulls a decision request from `qc-bridge`, asks a
  decider (fake, replay, Claude Agent SDK or OpenRouter), and answers with
  `submit_order_intent` or `no_trade`. That is the only way an agent can trade. It never holds a
  broker tool or credential ([ADR-0040](docs/adr/0040-agentic-decision.md)).
- **Control surface** (`agent/src/control/`): one small API for status, rules, the decision
  ledger, order intents and the kill switch, used by the web UI and the MCP server.
- **Broker interface** (`agent/src/broker/`): a generic `BrokerAdapter` plus an approval-verifying
  gateway, journal and reconciliation, tested against a synthetic mock broker. **No real broker
  adapter ships with this repository;** you write your own
  ([guide](docs/brokers/writing-a-broker-adapter.md)).
- **Research stack** (`research/`, `ml/`, `backtest/`): registered studies, walk-forward
  validation, an append-only experiment registry, and a graveyard for failed ideas.

## Architecture

```
  you / your agent ──► web UI ─┐
  (Claude, Cursor, …) ► MCP ───┤  status · rules (tighten-only) · ledger · kill switch
                               ▼
                     control API (agent/src/control)
                               │
   market data ──► qc-bridge ◄─┴── agent service (agent/) ◄── decider: fake | replay | LLM
                      │            only submit_order_intent / no_trade
                      ▼
          ┌─────── engine/ (Rust, deterministic, no network, no LLM) ───────┐
          │  risk gate (qc-risk) ── kill switch ── OMS ── order log/replay  │
          └──────────────────────────────┬──────────────────────────────────┘
                                         ▼  signed approvals only
                     SimVenue (default)  │  or  broker gateway ──► your BrokerAdapter
                                         ▼
                              append-only decision ledger
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quickstart

Prerequisites: Rust (stable), [uv](https://docs.astral.sh/uv/), [just](https://just.systems/),
Node.js 20+ and git. Optional: cargo-deny, cargo-audit, pip-audit.

```sh
git clone https://github.com/OWNER/quant-core.git && cd quant-core
just setup        # fetch Rust crates and the research Python environment
just agent-setup  # install the agent service's pinned npm packages
just check        # fmt, lint, every test suite, audits
just replay       # replay a synthetic market day twice; identical order-log hashes
just agent-sim    # agent (fake decider) → qc-bridge → risk → SimVenue, twice, same ledger hash
```

None of this needs broker credentials or makes a paid call. See
[docs/getting-started.md](docs/getting-started.md) for a guided first hour.

### Web UI

```sh
just control-api  # starts the local control API and serves the UI on localhost
```

Open <http://127.0.0.1:7070> (set `QC_CONTROL_PORT` to change it) to see engine status, your
limits, the decision ledger and the kill switch. It binds to 127.0.0.1 only.

### Let your agent drive it (MCP)

```sh
just mcp          # runs the quant-core MCP server over stdio
```

Add it to your agent's MCP config, for example `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "quant-core": {
      "command": "just",
      "args": ["mcp"]
    }
  }
}
```

Tools: `get_status`, `get_rules`, `propose_rule_change` (tighten-only), `submit_order_intent`
(checked by qc-bridge's risk gate; set `QC_BRIDGE_BIN` to route intents to a bridge),
`list_decisions`, `list_orders` and `engage_kill_switch`. Your agent cannot loosen a limit,
bypass the risk gate, or reach a broker directly. Details:
[docs/agent-interface.md](docs/agent-interface.md).

## Strategy lifecycle

Every strategy moves through these gates in order. Skipping one is a blocking error.

```
idea → research → backtest → walk-forward → paper → canary (tiny capital) → scaled
```

Each strategy's current gate lives in `strategies/<name>/README.md`. Start from
`strategies/_template/` and read [docs/strategies/writing-a-strategy.md](docs/strategies/writing-a-strategy.md).

## Safety model

- **Kill switch first.** Every live process honours the global kill switch within one second,
  even if the agent is stuck. CI tests this.
- **Tighten-only limits.** An agent can make a limit stricter at runtime. Loosening one is a
  human change to `config/limits/` that needs maintainer review.
- **No LLM in the engine.** `engine/` is deterministic and offline. Replaying a recorded day must
  reproduce identical orders; a replay failure blocks merge.
- **One path to a venue.** The agent can only call `submit_order_intent`. The engine's risk
  checks stay authoritative, and the broker gateway only sends orders the engine signed.
- **Never give an agent withdrawal rights.** Broker keys are trade-only, withdrawal-disabled,
  IP-allowlisted and scoped per strategy and environment. No process that holds broker
  credentials exposes them to the agent.
- **No secrets in git.** Keys live in a secret manager; `.env` is for local development only.

The full rules are in [CLAUDE.md](CLAUDE.md).

## Repo map

```
engine/      Rust: core, orderbook, gateway, oms, risk★, bridge, replay, telemetry
agent/       TypeScript agent service, control API + MCP server, broker interface, recorder
ui/          Web UI served by `just control-api`
schemas/     Versioned engine ↔ agent contract (schemas/decision/v1)
strategies/  One folder per strategy; gate status in each README
research/    Python research (uv project); sandbox/ for automated factor discovery
ml/          Model pipeline: train → validate → registry → shadow → promote
backtest/    Fill and latency models, configs, reports
data/        Schemas, recorders, catalog, data-quality checks
evals/       Deterministic and gated live-LLM eval suites for the agent
config/      limits★, venues, environments (no secrets)
ops/         Infra, deploy★, monitoring
agents/      Offline agent swarm; outputs are PRs and reports only
docs/        Architecture, ADRs, runbooks, glossary, research graveyard
autonomy/    ★ Policy for what automated jobs may do
★ = protected: maintainer review required
```

## Documentation

[Getting started](docs/getting-started.md) · [Architecture](docs/ARCHITECTURE.md) ·
[ADRs](docs/adr/) · [Runbooks](docs/runbooks/) ·
[Writing a strategy](docs/strategies/writing-a-strategy.md) ·
[Writing a broker adapter](docs/brokers/writing-a-broker-adapter.md) ·
[Research cycle](docs/process/research-cycle.md) · [Glossary](docs/GLOSSARY.md)

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md), the
[Code of Conduct](CODE_OF_CONDUCT.md) and [GOVERNANCE.md](GOVERNANCE.md). Report
vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE). Copyright quant-core contributors.
