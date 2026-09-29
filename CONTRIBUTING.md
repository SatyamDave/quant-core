# Contributing to quant-core

Thanks for helping. quant-core is a template people use to let an agent trade under hard rules, so
the bar is correctness and safety first, features second. Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md). How decisions get made is in [GOVERNANCE.md](GOVERNANCE.md).

## Development setup

Prerequisites: Rust (stable), [uv](https://docs.astral.sh/uv/), [just](https://just.systems/),
Node.js 20+ and git. Optional but used by `just audit`: cargo-deny, cargo-audit, pip-audit.

```sh
git clone https://github.com/<you>/quant-core.git && cd quant-core
just setup        # Rust crates + research/.venv from uv.lock
just agent-setup  # npm ci in agent/
just check        # must pass on a clean clone
```

Everything runs offline: no broker credentials, no paid API calls. The full walkthrough is in
[docs/getting-started.md](docs/getting-started.md).

## Workflow

1. Open or find an issue first for anything non-trivial, so work is not duplicated.
2. Branch from `main` as `<type>/<short-description>` (`feat/`, `fix/`, `docs/`, `refactor/`, ...).
3. Keep one concern per PR, ideally one crate or directory.
4. Run `just check` before you push. CI runs the same recipe, and a red check blocks merge.
5. Fill in the PR template honestly. A box you cannot tick means the PR is not ready.

### Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <summary>`,
for example `feat(engine/oms): add exhaustive order state machine`. The scope is usually the crate
or top-level directory. Keep commits small.

### Tests are required

- Every behaviour change comes with a test. A bug fix comes with a test that failed before the fix.
- Touching the engine hot path (`engine/crates/{core,orderbook,gateway,oms,strategy-runtime,inference}`)
  means running `just bench` and calling out any regression over 5%.
- `just replay` must still reproduce the committed hash. A determinism break blocks merge.
- Update docs for changed behaviour: the nearest `README.md` or `CLAUDE.md`, a runbook, or the glossary.

| Command | What it runs |
|---|---|
| `just check` | fmt check, clippy `-D warnings`, ruff, mypy, every test suite, dependency audits |
| `just test` | Rust tests, then each pytest suite (research, hooks, knowledge, autonomy, ci, evals, ops, studies) |
| `just agent-test` | the agent service's vitest suite (`agent/`) |
| `just replay` | engine determinism on the synthetic day |
| `just agent-sim` | agent → qc-bridge → risk → SimVenue, twice, identical ledger hash |
| `just eval` | the free, deterministic Tier 1 eval suite |

## Protected zones

These paths need an explicit maintainer review, not only green CI:

```
engine/crates/risk/**
config/limits/**
ops/deploy/**
fund/**
.github/workflows/**
.claude/hooks/**
.claude/settings.json
autonomy/**
```

A PR may never weaken a risk check, the kill switch or the replay tests. Loosening a numeric limit
needs two maintainer approvals. `.claude/hooks/guard-protected.sh` blocks Claude Code from
editing these paths unless explicitly allowed, and refuses any diff that loosens a limit in
`config/limits/`. It does not bind humans or CI, so enable branch protection and CODEOWNERS on
your fork too ([docs/runbooks/github-hardening.md](docs/runbooks/github-hardening.md)).

## Adding things

### A strategy

Copy `strategies/_template/` to `strategies/<name>/` (or use the `new-strategy` skill), fill in
every README field, and start at the `idea` gate. Gates are passed in order:
idea → research → backtest → walk-forward → paper → canary → scaled. Record every experiment,
including failures, in the registry. Check `docs/research/graveyard.md` before proposing an idea.
Guide: [docs/strategies/writing-a-strategy.md](docs/strategies/writing-a-strategy.md).

### A broker adapter

Implement `BrokerAdapter` (`agent/src/broker/adapter.ts`) in its own module, outside
`agent/src/decider/`. Fail closed on any response you cannot parse, send the gateway's `refId` as
the idempotency key, and keep credentials and account ids inside the adapter. Test it against the
mock broker first, then your broker's sandbox. Adapters that need live credentials to test are
best kept in your own fork. Guide:
[docs/brokers/writing-a-broker-adapter.md](docs/brokers/writing-a-broker-adapter.md).

### A decider

A decider implements `Decider` (`agent/src/decider/types.ts`): it takes a `DecisionRequest` and
returns a trade or `no_trade` decision. It never imports from `agent/src/broker/` (a test enforces
this) and never gets a broker tool. Wire it in `agent/src/cli.ts` behind a `QC_AGENT_MODE` value,
add tests under `agent/tests/`, and run `just eval`. A decider that calls a paid model must be off
by default in CI.

## Never commit

- Secrets of any kind: API keys, tokens, private keys, `.env` files. Pre-commit and CI scan for
  them. If one leaks, rotate it at once; deleting the commit is not enough.
- Personal or account data: broker account numbers, statements, positions, order history, real
  recorded market data from your account, or names and emails in fixtures.
- Recorded vendor data you have no right to redistribute. Put real recordings under `data/raw/`,
  which is gitignored.

## Using an AI coding agent here

The root [CLAUDE.md](CLAUDE.md) and nested `CLAUDE.md` files hold the rules an agent session
should follow. Prefer the skills in `.claude/skills/` over improvising a procedure. Treat the rules
as guidance; the enforcement is the tests, hooks and review.

## Licensing

By contributing you agree that your contribution is licensed under the
[Apache-2.0 license](LICENSE). New dependencies must follow
[ADR-0002](docs/adr/0002-dependencies-and-licenses.md): no GPL, AGPL, SSPL or unlicensed code.
