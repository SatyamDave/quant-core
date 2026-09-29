# evals

Three tiers, cheapest first (`llm-trading-eval-research.md` §5: recorded-response replay per PR,
a frozen scenario suite per merge, prospective shadow last). Root CLAUDE.md rule 6: every
experiment is recorded, including failures; a check that could not run is a skip, never a pass.

## Tier 1 — every PR, free, deterministic

`just eval` runs `scripts/eval/run.py`, which executes named cases and writes
`out/eval/report.json` and `out/eval/report.md` (both gitignored, uploaded as a CI artifact),
exiting nonzero on any failure. Cases today:

| Case | What it checks |
|---|---|
| `replay_determinism` | `just replay`: replaying `tests/replay/sample_day.csv` twice through `qc-replay` reproduces the committed order-log sha256 (root CLAUDE.md rule 7). |
| `risk_invariants` | `cargo test -p qc-risk` (pre-trade checks, protected zone), the chaos suite (`engine/crates/replay/tests/chaos.rs`: kill switch, disconnect, stale book, reconciliation, daily loss), and `cargo test -p qc-bridge` (kill-switch and oversized-order rejection asserted against the in-process `BridgeEngine`). |
| `risk_invariants_bridge_sessions` | `scripts/eval/bridge_sessions.py`: oversized order, an order-rate burst, and stale market data, each driven as a real JSON-Lines session against the *compiled* `qc-bridge` binary (a black-box check across the process boundary). The stale-data case also reruns the identical scenario against a deliberately loosened `stale_data_ms` and asserts it then accepts — proof this case would fail loudly if the real limit were ever weakened the same way (root CLAUDE.md rule 4). |
| `walkforward_demo_regression` | `just walkforward demo`'s deflated Sharpe and PBO stay within the tolerance committed in `evals/baselines/walkforward-demo.json`. **This is a regression check, not a performance claim**: `GATE_DSR=1.0` in `ml/validation/walkforward.py` is designed to be unreachable today, so verdict `FAIL` is the correct, expected result. The case exists to catch an unintended change to the walk-forward math or the synthetic data, not to bless a return. |
| `registry_integrity` | `research/tests/test_registry.py`: no duplicate `(experiment, config_hash)` rows in the committed trial ledger, and no second `trials.jsonl` anywhere under `research/registry/`. |
| `study_0001_guard` | `studies/0001-favorite-longshot/study.py` refuses to run without its exact snapshot. That snapshot is gitignored and kept outside git, so on any clean checkout or CI runner it is always absent — exit code 2 is the expected, correct result, proving the guard still fires. |
| `agent_sim_fake` | If the `agent-sim` recipe exists (it arrives from `agentic/agent-service`): runs it twice in fake mode, requires identical `out/agent/ledger.jsonl` hashes and zero ledger-schema violations, then runs `just agent-test`. Until that recipe exists, this case is recorded as **skipped** — `"not yet available (depends on agentic/agent-service)"` — never as a pass or a failure. |

A skip is listed in the report separately from a pass and never turns a failing run green; a
failure elsewhere is never hidden by a skip. See `scripts/eval/run.py`'s docstring for the exact
contract (`exit_code()`).

## Tier 2 — merge to main / nightly / manual dispatch, paid, gated

`scripts/eval/live_agent.py` runs the live agent over the frozen scenario set in
`evals/scenarios/` (four synthetic worldlines, one of them a deliberately adversarial price-shock
case; see `evals/scenarios/README.md`) and appends one result per scenario to
`evals/ledger/agent-evals.jsonl` (gitignored; a human decides whether to commit a run — see
`evals/ledger/README.md`).

It runs only if both hold:
- `python3 scripts/ci/ai_gate.py loop-agent-eval` allows (reads `autonomy/POLICY.yaml`, fails
  closed — same contract as every other paid loop in this repo, `scripts/ci/ai_gate.py`'s own
  docstring);
- `ANTHROPIC_API_KEY` is set.

Otherwise it exits 0 and prints `"skipped: gated off"` (with the reason), and that skip is
visible in the report, not silently swallowed. `loop-agent-eval` does not exist in
`autonomy/POLICY.yaml` yet — `agentic/agent-service` adds it, disabled and zero-budget — so this
tier is gated off by default (no `ANTHROPIC_API_KEY` ships with this repo).

**Baselines, next to every result, not the agent's score alone (issue #37).** Each scenario's
result reports the live agent's P&L next to `no_trade` (always 0), `buy_and_hold` (a fixed-size
position, sized to `config/limits/default.toml`'s `max_position`, entered at the scenario's first
quoted mid and marked at its last — computed via a real `qc-bridge` session, not a
reimplementation of order-book logic in Python), and `classifier_only` (this same scenario run in
`QC_AGENT_MODE=fake`: free, deterministic, no API key — this *is* the pre-agent fixed rule
ADR-0040 §5 describes, so it always runs regardless of the live gate). Per
`llm-trading-eval-research.md` §5 (FORESIGHT-9): an equal-weight buy-and-hold baseline beat LLM
agents in 31 of 36 frozen-scenario runs — the right prior is "the agent probably doesn't beat the
dumb baseline," not the reverse, and this is the check that proves it one way or the other on
every run rather than reporting the agent's own number in isolation.

The live and classifier-only runs call the agent CLI once each, directly — not through `just
agent-sim`, which unconditionally runs twice and demands identical ledger hashes. That
determinism check is right for fake mode but is not one a genuinely nondeterministic live run can
ever pass (ADR-0040 §5: "live mode cannot meet this bar"), so routing a live run through it would
have reported FAIL on every run's determinism alone, never on anything about the decision itself.

## Why paid evals don't run on every PR push

Two reasons, both evidenced in `llm-trading-eval-research.md`:

1. **Cost.** A live LLM call per PR push, multiplied by every push on every open PR, is
   unbounded spend for a check whose main job (catching a code regression) Tier 1's free,
   deterministic cases already do. `autonomy/POLICY.yaml`'s global and per-loop dollar caps exist
   precisely because an ungated paid loop can spend a day's budget on iteration noise, not signal.
2. **Nondeterminism.** An LLM agent's output is not reproducible run to run (`agent-sdk-research.md`:
   no temperature/seed knob on the Agent SDK), so a paid run on every push would be a flaky gate,
   not a stable one — and small-sample, single-split live-LLM comparisons are exactly the kind of
   result the Ordinal-Gates critique (§4 of the research) shows can look like a real effect and
   not survive a second split. Running it less often, against a frozen scenario set, with a fixed
   baseline to beat, is the cheap version of the mitigation the research recommends (§5,
   FORESIGHT-9: several frozen worldlines, not one window; a dumb baseline as the bar).

## Tier 3 — shadow, prospective (not built)

Documented here only; no code yet.

- **Design:** the agent decides on live data; orders go only to `SimVenue`/paper, never live
  capital (root CLAUDE.md rules 3-5, agentic-spec.md's recursive-learning-loop section). Every
  decision, outcome and gate reason is recorded to the decision ledger (`agentic-spec.md`'s
  handoff protocol v1), the same append-only discipline as the Tier 2 ledger.
- **Forward-only.** Per `llm-trading-eval-research.md` §1: a passive backtest cannot separate
  genuine skill from an LLM's recency/memorization confound (Zhang & Stadie, arXiv:2608.02985;
  Gao/Jiang/Yan, arXiv:2512.23847). The only mitigation with no residual leakage channel is
  evaluating strictly on data collected as it happens, after the fact of the decision, never
  replayed against pre-cutoff history as if it were a backtest.
- **Pre-planned sample size**, decided before the data is collected so the stopping rule cannot
  be chosen post hoc: `n ≈ 16·p·(1-p)/δ²` at the conventional 80% power / 5% significance,
  where `p` is the current champion's baseline rate for the metric being compared and `δ` the
  smallest improvement worth acting on (§5 of the research; the same formula
  `docs/plans/0002-autonomy.md`-style phase work would use for a promotion decision).
- **Holm correction** across every simultaneous comparison in a shadow run (candidate vs.
  champion vs. no-trade vs. buy-and-hold, per scenario) before any one of them is called
  significant — per the Ordinal-Gates community critique (§4): an uncorrected single-split result
  can look like a real effect (their headline `+9.2pp/yr`) and evaporate under correction
  (`+1.17pp`, not significant). Romano-Wolf is an acceptable stronger alternative where the
  comparisons are many and correlated.
- **Forward-only, not "mostly forward-only."** No pre-cutoff data is reused as if it were a
  shadow result, and no result is reported until the pre-planned sample size is reached — a
  peek-and-stop is exactly the failure mode the sample-size plan exists to prevent.

## Learning loop: scenarios from history, and champion/challenger promotion (issues #52, #53)

Two scripts close the "recursive learning loop" the coordinator spec describes: real decisions
become new test cases, and only agent changes that prove better on *both* a frozen test and a
real forward test get recommended.

**`just scenarios-from-ledger`** (`scripts/eval/scenarios_from_ledger.py`) reads a decision
ledger and, for every entry whose outcome is now known (at least `--horizon`, default 20, later
entries exist to mark against), applies three documented selection rules — a risk-rejected
trade, a confident decision that lost money, or a declined request that would have made money —
and writes a new frozen scenario under `evals/scenarios/from_ledger/`: a single-snapshot
recording reproducing the entry's own top-of-book, plus a `.provenance.json` sidecar (source
ledger sha256, entry index/request_id, date range, selection reason, recorded outcome, and the
original decision/result for human audit). The generator function that builds the scenario's CSV
takes only the entry's `request` — never its `decision`/`result`/outcome — so the recorded
outcome cannot leak into the generated scenario's decision inputs by construction, not by
filtering (`scripts/eval/scenarios_from_ledger.py`'s own docstring; tested directly). Labelled
`synthetic` or `real` by the source ledger entry's mode. Not wired into `live_agent.py`'s paid
Tier 2 scenario set (a human reviews generated scenarios first) — but `promote.py`'s frozen suite
below does include this directory by default, which is the loop actually closing.

**`just promote`** (`scripts/eval/promote.py`) is a champion/challenger pipeline for agent
prompt/tool-config versions (`agent/src/prompts/*.md`, `decider/fake.ts`'s decision threshold).
Two tiers, both required to promote:
1. **Frozen** — champion and challenger each run once per scenario in `evals/scenarios/*.csv`
   plus `evals/scenarios/from_ledger/*.csv`, in fake mode by default (deterministic, free), or
   live mode when `--live` is passed and `ai_gate.py loop-agent-eval` allows. Compared paired per
   scenario: net P&L after costs, plus two guardrails (invalid decisions, risk rejects) the
   challenger must not make significantly worse.
2. **Forward** — an already-recorded shadow/paper ledger for each candidate (issue #48's
   mechanism produces these, not this script); without both, this tier is `UNAVAILABLE`, never a
   silent pass.

Every metric's significance is a bootstrap test (paired for the frozen tier, unpaired for the
forward tier), Holm-corrected jointly across the metric family — per
`llm-trading-eval-research.md` §4's Ordinal-Gates critique, an uncorrected single test can look
like a real effect and evaporate under correction. **A challenger is `PROMOTED` only when both
tiers `PASS`** — helping on the frozen suite alone is `NOT_PROMOTED` (tested directly:
`tests/evals/test_promote.py::test_frozen_pass_forward_unavailable_is_not_promoted`). Every run
appends one side-by-side record to `evals/promotions/promotions.jsonl`; a `NOT_PROMOTED` verdict
also appends a row to `evals/graveyard.md`. A `PROMOTED` verdict writes *only* a recommendation
file under `evals/promotions/recommendations/` — **this script never edits
`agent/src/prompts/*.md` or any decider default itself** (tested at the filesystem level). If
champion and challenger report the same underlying model, the record always says so and why that
means their agreement is not independent confirmation (correlated-errors caution,
`llm-trading-eval-research.md` §2). Promotion-bar thresholds (minimum improvement, significance
level, bootstrap size) are config, not magic numbers: `evals/promotions/policy.toml`, placeholder
values pending the maintainers' decision, the same pattern issue #22 established for the classifier's
gate.
