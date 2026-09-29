# agent/ — the trade-decision service (ADR-0040)

TypeScript service that receives a `DecisionRequest` from `qc-bridge`, decides
`buy`/`sell`/`no_trade`, submits that decision back through `qc-bridge`'s risk-wrapped path, and
appends every decision to an append-only ledger. See
[`docs/adr/0040-agentic-decision.md`](../docs/adr/0040-agentic-decision.md) for why this step
exists and what it is and isn't authoritative over (short version: it proposes, `qc-bridge`'s
deterministic risk engine and kill switch dispose). Directory rules, MUSTs and NEVERs live in
[`CLAUDE.md`](CLAUDE.md); this file is the human-facing overview and the live-mode runbook.

## The three modes

`QC_AGENT_MODE` selects the decider (`src/cli.ts`); the ledger's `mode` field on every entry
records which one produced it (`schemas/decision/v1/decision_ledger_entry.schema.json`).

| Mode | Decider | Network / API key | What it's for |
|---|---|---|---|
| `fake` (default) | `src/decider/fake.ts`, a fixed rule over the classifier's signal | none | CI, `just agent-sim`, local development. |
| `replay` | `src/decider/replay.ts`, replays a committed fixture verbatim by `request_id` | none | Deterministic regression tests (issue #36) — see [`fixtures/replay/README.md`](fixtures/replay/README.md). |
| `live` | `src/decider/live.ts`, `@anthropic-ai/claude-agent-sdk` | `ANTHROPIC_API_KEY`, real spend | The actual agent. **Gated off by default; see below.** |

## Live mode is not enabled, and this repo cannot enable it by itself

As of this writing, `QC_AGENT_MODE=live` has never made a real call in this repository: there is
no `ANTHROPIC_API_KEY` anywhere in it, and
`autonomy/POLICY.yaml`'s `loop-agent-eval` entry ships disabled at zero budget
(`{"enabled": false, "daily_usd": 0, "max_turns": 0, "timeout_minutes": 0}`). `src/decider/live.ts`
(`resolveLivePolicy`) checks both independently and fails closed if either is missing — a
misconfigured or partially-set-up environment denies, it never falls back to a default budget.

Turning it on is a decision for the maintainers (`autonomy/POLICY.yaml` and everything under
`autonomy/**` is a protected path, root `CLAUDE.md` rule 3's extension: **two human approvals to
change it**, not one). This section documents the exact edits so that decision is a small,
reviewable diff when the maintainers make it — it does not make it for them, and nothing in this PR
flips any of these values.

### 1. The repo secret

Add `ANTHROPIC_API_KEY` as a GitHub Actions repository secret (Settings → Secrets and variables →
Actions → "New repository secret", name `ANTHROPIC_API_KEY`) for any workflow that will run live
mode in CI. For a one-off manual run from an operator's own machine, export it directly in the
shell instead — root `CLAUDE.md` rule 2: never put it in a committed file, `.env` is
local-development-only and already gitignored, and never echo, log, or print it.

### 2. The `autonomy/POLICY.yaml` edit

Two changes, both inside the file's existing protected structure — nothing new needs to be added:

1. Top-level `"enabled": false` → `"enabled": true` (the global autonomy kill switch; this affects
   every loop in the file, not just this one — read the rest of `POLICY.yaml` before flipping it).
2. `loops["loop-agent-eval"]`, currently
   `{"enabled": false, "daily_usd": 0, "max_turns": 0, "timeout_minutes": 0, "open_pr_cap": 0}`,
   becomes `"enabled": true` with real, positive, finite numbers for `daily_usd`, `max_turns`, and
   `timeout_minutes` — `scripts/ci/ai_gate.py` denies (fail closed) if any of the three is missing,
   non-numeric, or `<= 0`. These three map directly onto `LivePolicy` in `src/decider/live.ts`:
   `daily_usd` → `maxBudgetUsd`, `max_turns` → `maxTurns`, `timeout_minutes` → `timeoutMinutes`,
   passed straight into the Agent SDK's `Options.maxBudgetUsd`/`maxTurns`/`abortController`
   deadline for that one decision. Pick numbers the maintainers have actually agreed on (a small
   daily spending budget) — there is no fixture or default
   this repository can suggest that substitutes for that conversation.

`ai_gate.py` also requires two things this repo's environment provides independently of the file
edit above: the repo variable `AUTONOMY_ENABLED` must be exactly the string `"true"` in the
process environment that runs it (a GitHub Actions repository *variable*, not secret, for CI; an
exported shell variable for a manual run), and `autonomy/PAUSE` must not exist.

### 3. One command

With the secret set, `AUTONOMY_ENABLED=true` in the environment, and the two `POLICY.yaml` values
above set for real, a single decision request through the fake bridge in live mode looks like:

```sh
cd agent
QC_AGENT_MODE=live \
ANTHROPIC_API_KEY=sk-... \
AUTONOMY_ENABLED=true \
QC_BRIDGE_BIN=node_modules/.bin/tsx \
QC_BRIDGE_ARGS="src/testkit/fake-bridge.ts --scenario tests/fixtures/scenario.json" \
  npm start
```

Point `QC_BRIDGE_BIN`/`QC_BRIDGE_ARGS` at the real `qc-bridge` binary instead of the fake one
(same pattern as the `just agent-sim` recipe, without its hardcoded `QC_AGENT_MODE=fake`) once
`engine/crates/bridge` is what you want the live run driving against. Every decision this
command makes is appended to `out/agent/ledger.jsonl` with `mode: "live"` and the full SDK
request/response under `raw` — see issue #65's own acceptance criteria: record the cost/latency
from that run, and consider promoting a few of its decisions into
[`fixtures/replay/`](fixtures/replay/README.md) as genuine (non-synthetic) replay fixtures.

### Turning it back off

Set `loops["loop-agent-eval"].enabled` back to `false` (or `daily_usd`/`max_turns`/
`timeout_minutes` back to `0`), or set the top-level `"enabled"` back to `false`, or create
`autonomy/PAUSE`, or unset `AUTONOMY_ENABLED` — any one of these alone makes `ai_gate.py` deny
again, and `resolveLivePolicy` throws before `query()` is ever called. `agent/tests/decider-live.test.ts`
asserts the all-zero/disabled default denies; if the maintainers re-enable it, add the mirror-image
assertion (budget back to zero denies again) as part of that PR, per issue #65's own "tested, not
assumed" bar.

## Prompt versioning

`src/decider/live.ts` exports `PROMPT_VERSION` (currently `"v1"`, matching
`src/prompts/decide-v1.md`); every ledger entry records it. A prompt content change that isn't a
typo fix is a T2/T3 change like any other code change (ADR-0040 §7) — bump `PROMPT_VERSION` and
add a new `decide-v<N>.md` rather than editing a version that has ever gone live, so a replay
fixture recorded under `v1` stays reproducible under `v1` forever.

## External venue mode (your broker)

When `qc-bridge` runs with `--venue external`, approved orders go through `src/broker/`'s gateway
to a broker adapter you provide (`QC_BROKER_MODULE`). No real adapter ships with this repository;
`src/broker/README.md` explains how to write one, and `just agent-sim-external` runs the whole
external pipeline against the synthetic mock broker (`src/testkit/mock-broker.ts`).
