#!/usr/bin/env python3
"""Tier 2 eval: paid, nondeterministic, gated. Runs on merge to main, nightly, and
workflow_dispatch (.github/workflows/eval.yml) — never on every PR (evals/README.md explains why:
cost and nondeterminism, per llm-trading-eval-research.md §5).

  scripts/eval/live_agent.py [--root DIR]

Runs only if BOTH hold, otherwise exits 0 with "skipped: gated off" printed and written to the
report (a skip here is never a pass, same convention as scripts/eval/run.py):
  - `python3 scripts/ci/ai_gate.py loop-agent-eval` allows (autonomy/POLICY.yaml, fail closed);
  - ANTHROPIC_API_KEY is set in the environment.

When allowed, it calls the agent CLI directly once per frozen scenario under evals/scenarios/
(evals/scenarios/README.md — synthetic worldlines sliced from tests/replay/sample_day.csv, one of
them a deliberately adversarial price-shock case), in QC_AGENT_MODE=live, and appends one result
line per scenario to evals/ledger/agent-evals.jsonl. That file is gitignored: this script's own
run only ever writes it as a CI artifact (out/eval upload); a human decides whether a result is
worth committing (root CLAUDE.md rule 6, evals/ledger/README.md).

Issue #37: every run reports the agent's result next to two free, deterministic baselines
computed on the same scenario — no_trade (always 0) and buy_and_hold (a fixed-size reference
position entered at the scenario's first quoted mid and marked at its last, sized to
config/limits/default.toml's max_position, computed via the real qc-bridge/orderbook, not a
reimplementation of book logic in Python) — plus a "classifier_only" baseline, which is simply
this same scenario run in QC_AGENT_MODE=fake (the deterministic rule-based decider ADR-0040 §5
describes as the pre-agent fixed rule): free, no API key, and always run regardless of the gate,
so a live result is never reported alone. Per llm-trading-eval-research.md §5 (FORESIGHT-9): an
equal-weight buy-and-hold baseline beat LLM agents in 31 of 36 frozen-scenario runs — the right
prior is "the agent probably doesn't beat the dumb baseline," not the reverse.

Bug fixed here: the live/classifier-only runs call the agent CLI directly instead of routing
through `just agent-sim`, which unconditionally runs twice and demands identical ledger sha256
hashes — a check a genuinely nondeterministic live-mode run can never pass (ADR-0040 §5 says as
much: "live mode cannot meet this bar"). Going through that recipe would have made a working live
run report FAIL every single time on the determinism check alone, never on anything about the
decision itself.

This is not a stub: the gate and the live call are both real. It is gated off in this repo state
because autonomy/POLICY.yaml has no loop-agent-eval entry yet (added by agentic/agent-service,
disabled and zero-budget even once added) and no ANTHROPIC_API_KEY exists — the
same "fail closed, not run" state scripts/ci/ai_gate.py reports for every other paid loop today.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOOP = "loop-agent-eval"
LEDGER = ROOT / "evals/ledger/agent-evals.jsonl"
SCENARIOS_DIR = ROOT / "evals/scenarios"
DEFAULT_LIMITS = ROOT / "config/limits/default.toml"
MODEL_JSON = ROOT / "ml/tests/fixtures/model.json"
MODEL_SHA256 = ROOT / "ml/tests/fixtures/model.sha256"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts/reports"))
import bridge_sessions  # noqa: E402
import pnl_report  # noqa: E402


def gate_allows(root: Path, loop: str = LOOP) -> tuple[bool, str]:
    """Fail closed: any error, or ai_gate.py denying, means "do not run" (same contract as
    scripts/ci/ai_gate.py itself: a check that could not run is not a check that passed)."""
    proc = subprocess.run(  # noqa: S603 - fixed local ai_gate.py invocation
        [sys.executable, str(root / "scripts/ci/ai_gate.py"), loop, "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def scenario_files(root: Path = ROOT) -> list[Path]:
    return sorted((root / "evals/scenarios").glob("worldline-*.csv"))


def _bridge_args(scenario: Path) -> str:
    # Resolved to absolute: this string is handed to the agent CLI with cwd=agent/, so a relative
    # scenario path would silently resolve against the wrong directory and qc-bridge would fail to
    # find its recording (surfacing confusingly as an EPIPE on the agent's first stdin write, not
    # as a clear "file not found").
    sha256 = MODEL_SHA256.read_text().strip()
    return (
        f"{scenario.resolve()} --limits {DEFAULT_LIMITS} --model {MODEL_JSON} "
        f"--model-sha256 {sha256}"
    )


def _run_agent_once(
    scenario: Path, root: Path, mode: str, extra_env: dict[str, str] | None = None
) -> Path:
    """Runs the agent CLI exactly once (no double-run determinism check — see module docstring)
    in the given mode over `scenario`, and returns the path to the ledger it wrote. Requires
    qc-bridge to already be built (see ensure_bridge_built)."""
    ledger_path = Path(tempfile.mkdtemp()) / "ledger.jsonl"
    env = {
        **os.environ,
        **(extra_env or {}),
        "QC_AGENT_MODE": mode,
        "QC_BRIDGE_BIN": str(root / "engine/target/release/qc-bridge"),
        "QC_BRIDGE_ARGS": _bridge_args(scenario),
        "QC_AGENT_LEDGER_PATH": str(ledger_path),
    }
    proc = subprocess.run(
        ["node_modules/.bin/tsx", "src/cli.ts"],  # noqa: S607
        cwd=root / "agent",
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"agent cli exited {proc.returncode}\n{proc.stdout}{proc.stderr}")
    return ledger_path


def _pnl_total(ledger_path: Path) -> str:
    entries = pnl_report.load_entries(ledger_path)
    result, _curve = pnl_report.compute(entries, fee_bps=Decimal("0"))
    return result.agent["total_pnl_after_fees_usd"]


def classifier_only_pnl(scenario: Path, root: Path = ROOT) -> str:
    """QC_AGENT_MODE=fake over the same scenario: deterministic, free, no API key. This *is* the
    pre-agent fixed rule ADR-0040 §5 describes ("follow the classifier's signal... else
    no_trade") — always run, regardless of the live gate, so a live result is never reported
    without this baseline next to it."""
    ledger_path = _run_agent_once(
        scenario, root, mode="fake", extra_env={"QC_FAKE_PROB_THRESHOLD": "0.5"}
    )
    return _pnl_total(ledger_path)


def _mid_bounds(scenario: Path, root: Path) -> tuple[Decimal, Decimal]:
    """First and last quoted mid price for `scenario`, from the real qc-bridge/order book (not a
    reimplementation of book logic in Python — see module docstring). Sends one
    next_decision_request per line of the file (a safe upper bound: at most one decision request
    per record) and keeps the first and last non-null result."""
    bridge_sessions.ensure_bridge_built(root)
    line_count = len(scenario.read_text().splitlines())
    requests = [
        {"v": 1, "id": str(i), "op": "next_decision_request"} for i in range(line_count + 5)
    ]
    responses = bridge_sessions.run_session(
        scenario.read_text(), requests, limits_path=DEFAULT_LIMITS, decide_every=1, root=root
    )
    mids = [
        Decimal(r["decision_request"]["mid"])
        for r in responses
        if r.get("decision_request") is not None
    ]
    if not mids:
        raise RuntimeError(f"{scenario}: never reached a synced, decision-ready book")
    return mids[0], mids[-1]


def _buy_and_hold_pnl_from_mids(first_mid: Decimal, last_mid: Decimal, qty: Decimal) -> str:
    """The arithmetic half of buy_and_hold_pnl, split out so it's testable without a real
    qc-bridge session: enter `qty` at `first_mid`, mark at `last_mid`, no fee (matching the free,
    deterministic baselines this is compared against)."""
    pos = pnl_report.apply_fill(pnl_report.Position(), "buy", qty, first_mid, Decimal(0))
    return str(pos.total_pnl_after_fees(last_mid))


def buy_and_hold_pnl(scenario: Path, root: Path = ROOT) -> str:
    """A fixed-size (config/limits/default.toml's max_position) reference position entered at the
    scenario's first quoted mid and marked at its last."""
    first_mid, last_mid = _mid_bounds(scenario, root)
    limits = tomllib.loads(DEFAULT_LIMITS.read_text())
    qty = Decimal(str(limits["max_position"]))
    return _buy_and_hold_pnl_from_mids(first_mid, last_mid, qty)


def run_scenario(root: Path, scenario: Path) -> dict:
    """Calls the live agent over one frozen scenario, next to the no_trade, buy_and_hold and
    classifier_only baselines (issue #37)."""
    classifier_total = classifier_only_pnl(scenario, root)
    bh_total = buy_and_hold_pnl(scenario, root)
    try:
        ledger_path = _run_agent_once(scenario, root, mode="live")
        agent_total = _pnl_total(ledger_path)
        exit_code = 0
        error = None
    except RuntimeError as e:
        agent_total = None
        exit_code = 1
        error = str(e)
    return {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "scenario": scenario.name,
        "mode": "live",
        "exit_code": exit_code,
        "error": error,
        "baselines": {
            "no_trade_pnl_usd": "0",
            "buy_and_hold_pnl_usd": bh_total,
            "classifier_only_pnl_usd": classifier_total,
        },
        "agent_pnl_usd": agent_total,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root: Path = args.root

    allowed, reason = gate_allows(root)
    if not allowed:
        print(f"skipped: gated off ({reason})")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("skipped: gated off (ANTHROPIC_API_KEY not set)")
        return 0

    scenarios = scenario_files(root)
    if not scenarios:
        print("skipped: gated off (no scenarios under evals/scenarios/)")
        return 0

    # Built once, up front: classifier_only_pnl/run_scenario spawn QC_BRIDGE_BIN directly and do
    # not build it themselves (unlike _mid_bounds, which goes through bridge_sessions and always
    # ensures it). CI's tier2 job has no separate "build qc-bridge" step before this script runs.
    built, detail = bridge_sessions.ensure_bridge_built(root)
    if not built:
        print(f"FAILED: could not build qc-bridge: {detail}", file=sys.stderr)
        return 1

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    with LEDGER.open("a", encoding="utf-8") as f:
        for scenario in scenarios:
            result = run_scenario(root, scenario)
            f.write(json.dumps(result) + "\n")
            status = "ok" if result["exit_code"] == 0 else "FAILED"
            print(
                f"{scenario.name}: {status} (exit {result['exit_code']}) "
                f"agent={result['agent_pnl_usd']} "
                f"buy_and_hold={result['baselines']['buy_and_hold_pnl_usd']} "
                f"classifier_only={result['baselines']['classifier_only_pnl_usd']} "
                f"no_trade={result['baselines']['no_trade_pnl_usd']}"
            )
            if result["exit_code"] != 0:
                failures.append(scenario.name)

    if failures:
        print(f"live agent eval FAILED for: {failures}", file=sys.stderr)
        return 1
    print(f"live agent eval: {len(scenarios)} scenario(s) ok; appended to {LEDGER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
