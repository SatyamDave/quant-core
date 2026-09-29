#!/usr/bin/env python3
"""Tier 1 eval harness: free, deterministic, runs on every PR (`just eval`).

  scripts/eval/run.py [--root DIR]

Runs a fixed list of cases — replay determinism, the risk-invariant suite, a walk-forward
regression against a committed baseline, registry integrity, the study-0001 snapshot guard, and
the agent-sim cases when that recipe exists (it lands from agentic/agent-service) — writes
out/eval/report.json and out/eval/report.md (both gitignored), and exits nonzero if any case
failed. A case that could not run because its dependency does not exist yet is recorded as
"skipped", never as a pass: skipped and passed are counted and reported separately, and a
skip never turns a nonzero exit into zero by itself, and a failure never turns zero into
nonzero because of a skip elsewhere. See evals/README.md for the tier design and why paid,
nondeterministic evals are not in this file.

Standard library only, matching scripts/ci/ai_gate.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import textwrap
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

PASS, FAIL, SKIP = "pass", "fail", "skip"

# The agent service validates every ledger row against the full schema with ajv before writing;
# this eval re-checks the schema's required top-level fields independently, read from the
# committed schema so the two can never drift.
LEDGER_SCHEMA = Path("schemas/decision/v1/decision_ledger_entry.schema.json")


def required_ledger_keys(root: Path) -> list[str]:
    return list(json.loads((root / LEDGER_SCHEMA).read_text())["required"])


@dataclass
class CaseResult:
    name: str
    status: str  # pass | fail | skip
    detail: str
    duration_s: float
    metrics: dict[str, Any] = field(default_factory=dict)


def _run(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - cmd is a fixed argv list from this module's cases
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else ""
        return subprocess.CompletedProcess(cmd, 124, out, f"{err}\ntimed out after {timeout}s")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def _tail(text: str, n: int = 40) -> str:
    return "\n".join(text.splitlines()[-n:])


def _within(value: float, baseline: float, tol: float) -> bool:
    return abs(value - baseline) <= tol


def _recipe_exists(root: Path, name: str) -> bool:
    text = (root / "justfile").read_text()
    return re.search(rf"(?m)^{re.escape(name)}\b[^\n]*:", text) is not None


def case_replay_determinism(root: Path) -> CaseResult:
    """`just replay`: replays tests/replay/sample_day.csv twice through qc-replay and checks both
    order logs match each other and the committed sha256 (root CLAUDE.md rule 7)."""
    start = time.monotonic()
    proc = _run(["just", "replay"], cwd=root, timeout=300)
    duration = time.monotonic() - start
    if proc.returncode == 0:
        return CaseResult(
            "replay_determinism",
            PASS,
            "deterministic, matches the committed order-log hash",
            duration,
        )
    return CaseResult(
        "replay_determinism",
        FAIL,
        f"just replay exited {proc.returncode}\n{_tail(proc.stdout + proc.stderr)}",
        duration,
    )


def case_risk_invariants(root: Path) -> CaseResult:
    """cargo test -p qc-risk (pre-trade risk checks, protected zone) plus the chaos suite
    (engine/crates/replay/tests/chaos.rs: kill switch, disconnect, stale book, reconciliation,
    daily loss), plus cargo test -p qc-bridge (kill_switch.rs, risk_rejection.rs and friends,
    asserted against the in-process BridgeEngine the compiled binary wraps). Three cargo
    invocations because these live in three different crates' integration tests."""
    start = time.monotonic()
    engine = root / "engine"
    risk = _run(["cargo", "test", "-p", "qc-risk", "--locked"], cwd=engine, timeout=600)
    chaos = _run(
        ["cargo", "test", "-p", "qc-replay", "--test", "chaos", "--locked"],
        cwd=engine,
        timeout=600,
    )
    bridge = _run(["cargo", "test", "-p", "qc-bridge", "--locked"], cwd=engine, timeout=600)
    duration = time.monotonic() - start
    if risk.returncode == 0 and chaos.returncode == 0 and bridge.returncode == 0:
        return CaseResult(
            "risk_invariants",
            PASS,
            "qc-risk unit tests, the chaos suite and qc-bridge's session tests all pass",
            duration,
        )
    detail = []
    if risk.returncode != 0:
        detail.append(
            f"cargo test -p qc-risk exited {risk.returncode}\n{_tail(risk.stdout + risk.stderr)}"
        )
    if chaos.returncode != 0:
        detail.append(
            f"cargo test -p qc-replay --test chaos exited {chaos.returncode}\n"
            f"{_tail(chaos.stdout + chaos.stderr)}"
        )
    if bridge.returncode != 0:
        detail.append(
            f"cargo test -p qc-bridge exited {bridge.returncode}\n"
            f"{_tail(bridge.stdout + bridge.stderr)}"
        )
    return CaseResult("risk_invariants", FAIL, "\n\n".join(detail), duration)


def case_risk_invariants_bridge_sessions(root: Path) -> CaseResult:
    """Issue #38: known-bad inputs (oversized order, an order-rate burst, stale market data)
    driven as real JSON-Lines sessions against the compiled `qc-bridge` binary — a black-box
    check across the process boundary, distinct from case_risk_invariants' cargo test runs.
    See scripts/eval/bridge_sessions.py for the scenario and the loosened-limit control that
    proves this case would fail loudly if a real limit were weakened. Kill-switch coverage stays
    in case_risk_invariants (cargo test -p qc-bridge): there is no wire op to engage the kill
    switch from outside the process, so that one can only be asserted in-process."""
    start = time.monotonic()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bridge_sessions  # loaded lazily so a missing binary does not break --help

    outcomes = bridge_sessions.run_all(root)
    duration = time.monotonic() - start
    failed = [(name, detail) for name, ok, detail in outcomes if not ok]
    detail_lines = "\n".join(f"{name}: {detail}" for name, _ok, detail in outcomes)
    if failed:
        return CaseResult(
            "risk_invariants_bridge_sessions",
            FAIL,
            "\n".join(f"{name}: {detail}" for name, detail in failed) + f"\n\nall:\n{detail_lines}",
            duration,
        )
    return CaseResult("risk_invariants_bridge_sessions", PASS, detail_lines, duration)


def case_walkforward_regression(root: Path) -> CaseResult:
    """`just walkforward demo`: a regression check, not a performance claim. GATE_DSR=1.0 in
    ml/validation/walkforward.py is designed to be unreachable, so verdict FAIL is correct and
    expected; this case only fails if the demo's deflated Sharpe or PBO drift outside the
    tolerance committed in evals/baselines/walkforward-demo.json, which would mean the
    walk-forward math or the synthetic data changed under it, not that the demo model suddenly
    has edge."""
    start = time.monotonic()
    baseline = json.loads((root / "evals/baselines/walkforward-demo.json").read_text())
    proc = _run(["just", "walkforward", "demo"], cwd=root, timeout=300)
    if proc.returncode != 0:
        duration = time.monotonic() - start
        return CaseResult(
            "walkforward_demo_regression",
            FAIL,
            f"just walkforward demo exited {proc.returncode}\n{_tail(proc.stdout + proc.stderr)}",
            duration,
        )
    models_path = root / "out/walkforward/demo/registry/models.jsonl"
    lines = [line for line in models_path.read_text().splitlines() if line.strip()]
    duration = time.monotonic() - start
    if not lines:
        return CaseResult(
            "walkforward_demo_regression", FAIL, f"{models_path} is empty after the run", duration
        )
    record = json.loads(lines[-1])
    metrics = record.get("metrics", {})
    verdict = record.get("verdict")
    problems = []
    if verdict != baseline["verdict"]:
        problems.append(f"verdict {verdict!r} != baseline {baseline['verdict']!r}")
    for key, tol in baseline["tolerance"].items():
        value = metrics.get(key)
        if value is None:
            problems.append(f"metric {key!r} missing from this run's model record")
            continue
        if not _within(float(value), float(baseline["metrics"][key]), float(tol)):
            problems.append(
                f"{key}={value} outside tolerance {tol} of baseline {baseline['metrics'][key]}"
            )
    observed = {"verdict": verdict, **{k: metrics.get(k) for k in baseline["tolerance"]}}
    if problems:
        return CaseResult(
            "walkforward_demo_regression", FAIL, "; ".join(problems), duration, metrics=observed
        )
    return CaseResult(
        "walkforward_demo_regression",
        PASS,
        f"verdict {verdict}, within tolerance of the committed baseline",
        duration,
        metrics=observed,
    )


def case_registry_integrity(root: Path) -> CaseResult:
    """research/tests/test_registry.py: no duplicate (experiment, config_hash) rows in the
    committed trial ledger, and no second trials.jsonl anywhere under research/registry/."""
    start = time.monotonic()
    proc = _run(
        ["uv", "run", "--locked", "pytest", "-q", "tests/test_registry.py"],
        cwd=root / "research",
        timeout=120,
    )
    duration = time.monotonic() - start
    if proc.returncode == 0:
        return CaseResult("registry_integrity", PASS, _tail(proc.stdout, 3) or "passed", duration)
    return CaseResult(
        "registry_integrity",
        FAIL,
        f"exited {proc.returncode}\n{_tail(proc.stdout + proc.stderr)}",
        duration,
    )


def case_study_0001_guard(root: Path) -> CaseResult:
    """studies/0001-favorite-longshot/study.py refuses to run unless the exact snapshot
    (data/raw/study-0001, gitignored, kept outside git) is present. On
    any clean checkout or CI runner that snapshot is always absent, so exit code 2 is the
    expected, correct result here: it proves the guard still fires, not that the study ran."""
    start = time.monotonic()
    proc = _run(
        ["uv", "run", "--locked", "python", "studies/0001-favorite-longshot/study.py"],
        cwd=root / "research",
        timeout=60,
    )
    duration = time.monotonic() - start
    if proc.returncode == 2:
        return CaseResult(
            "study_0001_guard", PASS, "snapshot absent; guard exited 2 as expected", duration
        )
    return CaseResult(
        "study_0001_guard",
        FAIL,
        f"expected exit 2 (missing-snapshot guard), got {proc.returncode}\n"
        f"{_tail(proc.stdout + proc.stderr)}",
        duration,
    )


def _ledger_schema_violations(ledger: Path, required: list[str]) -> list[str]:
    problems = []
    for i, line in enumerate(ledger.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append(f"line {i}: invalid JSON ({e})")
            continue
        missing = [k for k in required if k not in row]
        if missing:
            problems.append(f"line {i}: missing {missing}")
    return problems


def case_agent_sim(root: Path) -> CaseResult:
    """`just agent-sim` (agentic/agent-service: the TS agent service in fake mode against
    qc-bridge): run twice, require identical ledger hashes (out/agent/ledger.jsonl) and zero
    ledger-schema violations, then `just agent-test`. Recorded as skipped, never failed, until
    that lane's recipe exists on this branch."""
    start = time.monotonic()
    if not _recipe_exists(root, "agent-sim"):
        duration = time.monotonic() - start
        return CaseResult(
            "agent_sim_fake",
            SKIP,
            "not yet available (depends on agentic/agent-service)",
            duration,
        )
    ledger = root / "out/agent/ledger.jsonl"
    hashes = []
    for run_n in (1, 2):
        proc = _run(["just", "agent-sim"], cwd=root, timeout=300)
        if proc.returncode != 0:
            duration = time.monotonic() - start
            return CaseResult(
                "agent_sim_fake",
                FAIL,
                f"just agent-sim (run {run_n}) exited {proc.returncode}\n"
                f"{_tail(proc.stdout + proc.stderr)}",
                duration,
            )
        if not ledger.exists():
            duration = time.monotonic() - start
            return CaseResult(
                "agent_sim_fake", FAIL, f"{ledger} does not exist after just agent-sim", duration
            )
        hashes.append(hashlib.sha256(ledger.read_bytes()).hexdigest())
    if hashes[0] != hashes[1]:
        duration = time.monotonic() - start
        return CaseResult(
            "agent_sim_fake",
            FAIL,
            f"ledger hash differs between two fake-mode runs: {hashes[0]} != {hashes[1]}",
            duration,
        )
    violations = _ledger_schema_violations(ledger, required_ledger_keys(root))
    if violations:
        duration = time.monotonic() - start
        return CaseResult(
            "agent_sim_fake",
            FAIL,
            f"{len(violations)} ledger row(s) failed the structural schema check: "
            + "; ".join(violations[:5]),
            duration,
        )
    test_proc = _run(["just", "agent-test"], cwd=root, timeout=300)
    duration = time.monotonic() - start
    if test_proc.returncode != 0:
        return CaseResult(
            "agent_sim_fake",
            FAIL,
            f"just agent-test exited {test_proc.returncode}\n"
            f"{_tail(test_proc.stdout + test_proc.stderr)}",
            duration,
        )
    return CaseResult(
        "agent_sim_fake",
        PASS,
        f"identical ledger sha256 across two fake-mode runs ({hashes[0][:12]}...), "
        "0 schema violations, just agent-test passed",
        duration,
    )


CASES = (
    case_replay_determinism,
    case_risk_invariants,
    case_risk_invariants_bridge_sessions,
    case_walkforward_regression,
    case_registry_integrity,
    case_study_0001_guard,
    case_agent_sim,
)


def run_all(root: Path) -> list[CaseResult]:
    return [case(root) for case in CASES]


def exit_code(results: list[CaseResult]) -> int:
    """A skip never causes a nonzero exit by itself; any fail always does."""
    return 1 if any(r.status == FAIL for r in results) else 0


def write_report(root: Path, results: list[CaseResult]) -> tuple[Path, Path]:
    out_dir = root / "out/eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = Counter(r.status for r in results)
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "summary": {"pass": counts[PASS], "fail": counts[FAIL], "skip": counts[SKIP]},
        "cases": [asdict(r) for r in results],
    }
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n")

    rows = "\n".join(
        f"| {r.name} | {r.status.upper()} | {r.duration_s:.1f}s "
        f"| {r.detail.splitlines()[0] if r.detail else ''} |"
        for r in results
    )
    detail_sections = "\n".join(
        f"### {r.name} ({r.status.upper()})\n\n```\n{r.detail}\n```\n" for r in results if r.detail
    )
    md_path = out_dir / "report.md"
    md_path.write_text(
        f"# Tier 1 eval report\n\n"
        f"Generated {payload['generated_at']}. "
        f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped "
        "(a skip is not a pass; see evals/README.md).\n\n"
        "| Case | Status | Duration | Detail |\n|---|---|---|---|\n"
        f"{rows}\n\n## Detail\n\n{detail_sections}"
    )
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    results = run_all(args.root)
    json_path, _md_path = write_report(args.root, results)
    counts = Counter(r.status for r in results)
    for r in results:
        print(f"[{r.status.upper():4}] {r.name} ({r.duration_s:.1f}s)")
        if r.status == FAIL:
            print(textwrap.indent(r.detail, "    "))
    print(
        f"\n{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped; "
        f"report: {json_path.relative_to(args.root)}"
    )
    code = exit_code(results)
    if code:
        print(f"eval FAIL: {[r.name for r in results if r.status == FAIL]}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
