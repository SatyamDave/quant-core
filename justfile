# quant-core task runner. Run `just` to list recipes.

set shell := ["bash", "-euo", "pipefail", "-c"]

engine := "engine"
research := "research"
agent := "agent"
# Python outside research/ and ml/ that ruff also checks, relative to research/. Not listed:
# autonomy/ and tests/autonomy/ (protected paths that fail ruff today; fixing them needs a T3 PR),
# and tests/replay/generate_sample_day.py (S311, PTH123; the fixture it wrote must stay identical).
py_extra := "../data ../scripts ../tests/knowledge ../tests/hooks ../tests/ci ../tests/evals ../tests/ops ../tests/shadow ../ops/live ../.claude/hooks"

default:
    @just --list

# Install toolchains' dependencies for the engine and research environment
setup:
    cd {{engine}} && cargo fetch
    cd {{research}} && uv sync --locked

# Everything CI requires: format check, lint, tests, dependency audit
check: fmt-check lint test audit

# Rewrite files into canonical format
fmt:
    cd {{engine}} && cargo fmt --all
    cd {{research}} && uv run ruff format . ../ml {{py_extra}}

fmt-check:
    cd {{engine}} && cargo fmt --all --check
    cd {{research}} && uv run ruff format --check . ../ml {{py_extra}}

lint:
    cd {{engine}} && cargo clippy --workspace --all-targets --locked -- -D warnings
    cd {{research}} && uv run ruff check . ../ml {{py_extra}}
    cd {{research}} && uv run mypy registry ../ml

# One pytest run per suite so the log shows each suite's own pass count.
test:
    cd {{engine}} && cargo test --workspace --locked
    cd {{research}} && uv run pytest -q
    cd {{research}} && uv run pytest -q ../tests/hooks
    cd {{research}} && uv run pytest -q ../tests/knowledge
    cd {{research}} && uv run pytest -q ../tests/autonomy
    cd {{research}} && uv run pytest -q ../tests/ci
    cd {{research}} && uv run pytest -q ../tests/evals
    cd {{research}} && uv run pytest -q ../tests/ops
    cd {{research}} && uv run pytest -q ../tests/shadow
    cd {{research}} && uv run pytest -q studies/0001-favorite-longshot/tests
    cd {{research}} && uv run pytest -q studies/0002-favorite-longshot-v2/tests

# Known-vulnerability and license checks; each tool is skipped with a message if it is not installed
audit:
    #!/usr/bin/env bash
    set -euo pipefail
    # A skipped scanner is not a clean scan: locally a missing tool is a loud warning, in CI (CI=true) it fails.
    skip() { echo "SKIPPED (not a pass): $1" >&2; if [ -n "${CI:-}" ]; then exit 1; fi; }
    cd {{engine}}
    if command -v cargo-deny >/dev/null && [ -f deny.toml ]; then cargo deny check; else skip "cargo-deny or engine/deny.toml missing (install with 'cargo install cargo-deny')"; fi
    if command -v cargo-audit >/dev/null; then cargo audit --deny warnings; else skip "cargo-audit not installed (install with 'cargo install cargo-audit')"; fi
    cd ../{{research}}
    if command -v pip-audit >/dev/null; then uv export --format requirements-txt --no-hashes --quiet | pip-audit --strict -r /dev/stdin; else skip "pip-audit not installed (install with 'uv tool install pip-audit')"; fi

# Install the git pre-commit hook and run every hook once over the whole tree
precommit:
    uvx pre-commit install
    uvx pre-commit run --all-files

# Criterion benchmarks for engine hot paths
bench:
    cd {{engine}} && cargo bench --workspace --locked

# Replay the synthetic fixture day (tests/replay/sample_day.csv) twice and check both order logs match the committed sha256
replay:
    #!/usr/bin/env bash
    set -euo pipefail
    cd {{engine}}
    sha() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
    out=$(mktemp -d)
    cargo build --release --locked -q -p qc-replay
    for run in 1 2; do ./target/release/qc-replay ../tests/replay/sample_day.csv "$out/orders-$run.log" ../tests/replay/limits.toml; done
    first=$(sha "$out/orders-1.log"); second=$(sha "$out/orders-2.log")
    echo "run 1 order-log sha256: $first"
    echo "run 2 order-log sha256: $second"
    if [ "$first" != "$second" ]; then echo "FAIL: replay is not deterministic"; exit 1; fi
    expected=$(tr -d '[:space:]' < ../tests/replay/expected_order_log.sha256)
    if [ "$first" != "$expected" ]; then echo "FAIL: order log does not match tests/replay/expected_order_log.sha256 ($expected)"; exit 1; fi
    echo "replay deterministic and matches the committed order log"

# Build the qc-bridge release binary (protocol v1 stdio bridge, schemas/decision/v1)
bridge:
    cd {{engine}} && cargo build --release --locked -p qc-bridge

# Run a backtest from backtest/configs/<cfg>.yaml (not implemented: exits 2 so nothing mistakes it for a pass)
backtest cfg:
    @echo "not implemented: no execution backtest exists yet ({{cfg}}); 'just walkforward <name>' is the label-level evaluation" >&2
    @exit 2

# Run a strategy against live data with simulated fills
paper strategy:
    @echo "refused: paper trading needs live market data, and bootstrap allows no venue connections ({{strategy}})" >&2
    @exit 1

# Purged, embargoed walk-forward validation for a strategy or model.
# By default writes only to the gitignored out/walkforward/<name>/ (report, trials, challenger).
# `just walkforward <name> --record` writes backtest/reports/<name>/<date>.md and the canonical registry.
walkforward name *record:
    cd {{research}} && PYTHONPATH=..:. uv run --locked python -m ml.validation.walkforward {{name}} {{record}}

# Show untriaged session learnings
learnings:
    #!/usr/bin/env bash
    set -euo pipefail
    shopt -s nullglob
    items=(.claude/learnings/inbox/*.md)
    echo "${#items[@]} untriaged learning(s) in .claude/learnings/inbox"
    for f in ${items[@]+"${items[@]}"}; do echo "  $f: $(head -1 "$f" | sed 's/^# //')"; done

# Tests for the Claude Code hooks in .claude/hooks alone (also part of `test`)
test-hooks:
    cd {{research}} && uv run pytest ../tests/hooks -q

# Study 0001 (favorite-longshot bias): offline analysis from the saved snapshot, then its checks
study-0001:
    cd {{research}} && uv run python studies/0001-favorite-longshot/study.py
    cd {{research}} && uv run pytest studies/0001-favorite-longshot/tests -q

# Study 0002 (favorite-longshot bias v2): offline analysis from the saved snapshot, then its checks
study-0002:
    cd {{research}} && uv run python studies/0002-favorite-longshot-v2/study.py
    cd {{research}} && uv run pytest studies/0002-favorite-longshot-v2/tests -q

# Tier 1 eval suite: free, deterministic, every PR. Writes out/eval/report.{json,md}.
# See evals/README.md for the tier design (this is Tier 1 of 3).
eval:
    python3 scripts/eval/run.py

# Issue #39: render out/agent/ledger.jsonl into a short, readable markdown/JSON report
# (out/reports/ledger/report.{json,md}, both gitignored). Pass extra args, e.g.
# `just report-ledger --ledger some/other/ledger.jsonl`.
report-ledger *args:
    python3 scripts/reports/ledger_report.py {{args}}

# Issue #70: realized+unrealized P&L after fees vs no-trade and buy-and-hold, drawdown, AI cost,
# and a stop-rule check (scripts/reports/pnl_policy.toml — placeholder thresholds; set your own). Writes one append-only file per day to fund/track-record/daily/
# (fund/track-record/README.md documents the format). Nonzero exit means the stop rule tripped.
report-pnl *args:
    python3 scripts/reports/pnl_report.py {{args}}

# Issue #45 (scheduling) + #70: the once-a-day close. Runs (or checks) the day's broker
# reconciliation, writes the broker-sourced daily P&L (report-pnl --source broker) with that
# verdict embedded, and alerts (scripts/ops/alerts.py) on a reconcile mismatch or a stop-rule
# trip. No live host exists yet (ops/live/README.md), so without --reconcile-cmd this always
# reports reconciliation "unknown" and exits nonzero -- fails closed rather than claiming clean.
# See scripts/ops/daily_close.py's module docstring for the systemd-timer wiring.
daily-close *args:
    python3 scripts/ops/daily_close.py {{args}}

# Issue #50/#51: refuse to start live trading unless every go-live gate passes (ADR-0040 status,
# the operator go-live approval, canary caps, the autonomy policy, the kill switch, reconciliation, secrets,
# latency). Exit 1 and every failing gate printed in plain language if any gate fails.
preflight-live *args:
    python3 scripts/ops/preflight.py {{args}}

# Issue #52: turn decision-ledger entries with known outcomes into new frozen scenarios under
# evals/scenarios/from_ledger/ (provenance sidecar, synthetic/real label; never leaks the
# outcome into the generated scenario's decision inputs). No-op, exit 0, if no ledger exists yet.
scenarios-from-ledger *args:
    python3 scripts/eval/scenarios_from_ledger.py {{args}}

# Issue #53: champion/challenger promotion for agent prompt/tool-config versions. Runs the
# frozen suite (fake mode; --live only when scripts/ci/ai_gate.py allows) and, if given
# --champion-forward-ledger/--challenger-forward-ledger, a forward/shadow comparison; promotes
# only when both tiers pass. Never edits agent/src/prompts/*.md or any decider default — writes
# an append-only record to evals/promotions/ and, on PROMOTED, a recommendation file for
# the maintainers to act on by hand.
promote *args:
    python3 scripts/eval/promote.py {{args}}

# Issue #54: does a panel of N agents beat one, at the exact same total AI usage? Runs a single
# decider and an N-member ensemble (equal-budget by construction: agent/src/cli.ts splits one
# per-decision budget N ways, never approximated) over the frozen suite, fake mode by default
# (--live only when scripts/ci/ai_gate.py allows); reports the paired P&L difference, whether the
# panel's own members agreed with each other, and a plain ADOPT/DO_NOT_ADOPT verdict. Never
# promotes anything itself -- this is a research comparison, not the promotion pipeline (`just
# promote`); an ADOPT result is a maintainer decision to wire an ensemble into the live loop.
ensemble-compare *args:
    python3 scripts/eval/ensemble_compare.py {{args}}

# ADR-0040 agent service (agent/): install pinned dependencies (npm ci, committed lockfile)
agent-setup:
    cd {{agent}} && npm ci

# ADR-0040 agent service: offline unit tests only (bridge client, schema validation, fake
# decider, live-decider config/hook, ledger, full fake loop against a fake bridge) — no
# network, no API key, no real qc-bridge binary required
agent-test:
    cd {{agent}} && npm test

# End-to-end fake-mode demo: builds the real qc-bridge and drives it over
# tests/replay/sample_day.csv (set QC_BRIDGE_BIN to use another bridge, e.g. the TS stand-in
# src/testkit/fake-bridge.ts). Runs the loop twice and fails unless both ledger hashes match.
agent-sim:
    #!/usr/bin/env bash
    set -euo pipefail
    cd {{agent}}
    export QC_AGENT_MODE=fake
    if [ -z "${QC_BRIDGE_BIN:-}" ]; then
      # The real Rust qc-bridge over the synthetic sample day, with the parity-fixture model.
      # A build failure fails the recipe: a silent fallback to the fake bridge would hide it.
      (cd ../engine && cargo build --release --locked -p qc-bridge -q)
      model=../ml/tests/fixtures/model.json
      export QC_BRIDGE_BIN=../engine/target/release/qc-bridge
      export QC_BRIDGE_ARGS="../tests/replay/sample_day.csv --limits ../tests/replay/limits.toml --model $model --model-sha256 $(tr -d '[:space:]' < ../ml/tests/fixtures/model.sha256)"
      # Smoke threshold: low enough that the order path (risk, OMS, SimVenue) actually runs.
      export QC_FAKE_PROB_THRESHOLD="${QC_FAKE_PROB_THRESHOLD:-0.5}"
    fi
    sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
    run() {
      local out; out=$(mktemp -d)
      QC_AGENT_LEDGER_PATH="$out/ledger.jsonl" node_modules/.bin/tsx src/cli.ts >&2
      # Keep the latest ledger where evals and CI artifacts look for it.
      mkdir -p ../out/agent && cp "$out/ledger.jsonl" ../out/agent/ledger.jsonl
      sha "$out/ledger.jsonl"
    }
    hash1=$(run)
    hash2=$(run)
    echo "run 1 ledger sha256: $hash1"
    echo "run 2 ledger sha256: $hash2"
    if [ "$hash1" != "$hash2" ]; then echo "FAIL: agent-sim ledger is not deterministic"; exit 1; fi
    echo "agent-sim deterministic across two runs"

# Protocol v1.2 (wave2-spec.md, issue #45 + gateway-integration): agent (fake decider) -> the
# real qc-bridge --venue external -> gateway -> mock broker -> report_execution, run twice
# with identical ledger AND broker-journal hashes, exercising a partial fill, a reject, and a
# cancel. Set QC_BRIDGE_BIN to point at a different binary (e.g. the fake, for a unit-test-only
# run); the default is the real release binary built from engine/crates/bridge.
agent-sim-external:
    cd {{agent}} && node_modules/.bin/tsx src/testkit/run-external-sim.ts

# Issue #46: polls your broker's quotes for one instrument, writes a replay-store CSV under
# data/raw/ (gitignored) and a data/catalog/<dataset>.json manifest (agent/src/recorder/).
# Read-only: the recorder only ever holds the adapter's getQuote/getBook --
# agent/src/recorder/allowlist.ts enforces it, agent/tests/recorder/allowlist.test.ts proves it.
# `--dry-run` runs entirely against the in-process mock broker (no network); without it, loads
# your broker adapter from QC_BROKER_MODULE (agent/src/broker/README.md). `just record-quotes --dry-run` alone writes a small
# default-budget file; agent/tests/recorder/replay-integration.test.ts is what proves the output
# actually replays through qc-replay.
record-quotes *args:
    cd {{agent}} && node_modules/.bin/tsx src/recorder/cli.ts {{args}}

# Issues #31/#55: per-decision latency (raw bridge round trip, agent decide step, full loop) in
# fake and replay mode, N decisions each (default 1000). Prints p50/p95/p99/max/throughput and
# writes out/bench/latency-results.json (gitignored). See docs/reports/latency.md for the last
# recorded run and the "first HFAT" definition it supports.
bench-latency *args:
    python3 scripts/bench/latency.py {{args}}

# Issue #48 (A5 Shadow, infrastructure): recorder -> qc-bridge --follow (sim venue only) -> agent
# (`--decider fake|live|openrouter`, default fake; live and openrouter gated by
# scripts/ci/ai_gate.py; `--live` = `--decider live`; `--symbol X` gives qc-bridge
# config/instruments/<x>.toml) -> daily report, all as one command for
# one calendar day (`--date`, default today UTC). `--dry-run` runs the whole pipeline against
# the mock broker with a small, fast budget; a real run needs your broker adapter
# (QC_BROKER_MODULE, agent/src/broker/README.md) plus an explicit
# --request-budget (scripts/shadow/run.py refuses to guess one). Writes
# out/shadow/<date>/{ledger.jsonl,heartbeat.jsonl,report/} (gitignored; a shadow run is
# simulated and is never a broker-sourced trade record).
shadow *args:
    python3 scripts/shadow/run.py {{args}}

# Writes config/instruments/<symbol>.toml for one US stock: spy.toml's shape (tick 0.01, whole
# shares, NYSE hours and calendar), the next free id, and spy.toml's own limits file, so caps can
# never be looser than SPY's. SYMBOL: 1-6 uppercase letters, optional dot suffix (BRK.B).
new-equity-instrument symbol:
    python3 scripts/ops/new_equity_instrument.py {{symbol}}

# Live canary trading of one stock with real orders (docs/runbooks/first-trading-day.md). Runs
# every preflight gate first (--decider openrouter) and refuses unless all pass; then recorder +
# supervisor -> agent (openrouter) -> qc-bridge --venue external --kill-file ops/live/state/KILL
# until the 16:00 ET close, then the daily close. `--mock` runs the same gates on fixtures and
# the chain against the mock broker and a mock OpenRouter server only.
live-canary symbol *args:
    python3 scripts/ops/live_canary.py {{symbol}} {{args}}

# Set QC_BRIDGE_BIN/QC_BRIDGE_ARGS to route intents to a bridge; stdout carries only JSON-RPC,
# so the recipe line is not echoed. Tools: get_status, get_rules, propose_rule_change
# (tighten-only), submit_order_intent (qc-bridge risk checks), list_decisions, list_orders,
# engage_kill_switch.
# MCP stdio server for your own agent (docs/agent-interface.md)
mcp:
    @cd {{agent}} && node_modules/.bin/tsx src/control/mcp-server.ts

# Same operations as `just mcp` under /api/*, plus the UI's static files (ui/dist, else ui/) at /.
# Localhost-only JSON API for the UI (127.0.0.1, QC_CONTROL_PORT, default 7070)
control-api:
    cd {{agent}} && node_modules/.bin/tsx src/control/http-server.ts
