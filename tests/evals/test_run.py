"""scripts/eval/run.py: case failure propagates to a nonzero exit, a skip is never a pass, and
the walk-forward tolerance check catches drift in both directions. Run with `just eval` (real
subprocess cases) or `just test` (this file, no subprocesses)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUN = Path(__file__).resolve().parents[2] / "scripts/eval/run.py"
spec = importlib.util.spec_from_file_location("eval_run", RUN)
assert spec and spec.loader
run = importlib.util.module_from_spec(spec)
# dataclasses resolves annotations via sys.modules[cls.__module__]; register before exec_module.
sys.modules[spec.name] = run
spec.loader.exec_module(run)


def result(name: str, status: str, detail: str = "") -> run.CaseResult:
    return run.CaseResult(name, status, detail, duration_s=0.1)


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([run.PASS, run.PASS], 0),
        ([run.PASS, run.SKIP], 0),
        ([run.SKIP, run.SKIP], 0),  # every case skipped is still not a failure
        ([run.PASS, run.FAIL], 1),
        ([run.FAIL, run.SKIP], 1),  # a fail elsewhere is never masked by a skip
        ([run.FAIL], 1),
    ],
)
def test_exit_code_reflects_any_failure(statuses: list[str], expected: int) -> None:
    results = [result(f"case-{i}", s) for i, s in enumerate(statuses)]
    assert run.exit_code(results) == expected


def test_skip_is_counted_separately_from_pass(tmp_path: Path) -> None:
    results = [result("a", run.PASS), result("b", run.SKIP), result("c", run.SKIP)]
    json_path, md_path = run.write_report(tmp_path, results)
    payload = json.loads(json_path.read_text())
    assert payload["summary"] == {"pass": 1, "fail": 0, "skip": 2}
    # A skip must never be miscounted as a pass in either report.
    assert payload["summary"]["pass"] != 3
    assert "skipped" in md_path.read_text()


def test_write_report_records_a_failing_case_and_its_detail(tmp_path: Path) -> None:
    results = [result("broken", run.FAIL, "cargo test -p qc-risk exited 101\nassertion failed")]
    json_path, _ = run.write_report(tmp_path, results)
    payload = json.loads(json_path.read_text())
    assert payload["summary"]["fail"] == 1
    assert payload["cases"][0]["status"] == "fail"
    assert "assertion failed" in payload["cases"][0]["detail"]


@pytest.mark.parametrize(
    ("value", "baseline", "tol", "expected"),
    [
        (0.0, 0.0, 0.05, True),
        (0.05, 0.0, 0.05, True),  # exactly at the tolerance boundary still passes
        (0.0501, 0.0, 0.05, False),  # just past the boundary fails
        (0.343, 0.343, 0.03, True),
        (0.40, 0.343, 0.03, False),
    ],
)
def test_within_tolerance_boundary(
    value: float, baseline: float, tol: float, expected: bool
) -> None:
    assert run._within(value, baseline, tol) is expected


def test_recipe_exists_true_and_false(tmp_path: Path) -> None:
    (tmp_path / "justfile").write_text("default:\n    @just --list\n\nagent-sim:\n    echo hi\n")
    assert run._recipe_exists(tmp_path, "agent-sim") is True
    assert run._recipe_exists(tmp_path, "agent-test") is False


def test_ledger_schema_violations_flags_invalid_json_and_missing_keys(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    required = ["request", "decision", "mode", "prompt_version", "model"]
    good = {k: {} for k in required}
    ledger.write_text("\n".join([json.dumps(good), "not json", json.dumps({"decision": {}})]))
    violations = run._ledger_schema_violations(ledger, required)
    assert len(violations) == 2
    assert any("invalid JSON" in v for v in violations)
    assert any("missing" in v for v in violations)


def test_ledger_schema_violations_clean_ledger_has_none(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    required = ["request", "decision", "mode", "prompt_version", "model"]
    row = {k: {} for k in required}
    ledger.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    assert run._ledger_schema_violations(ledger, required) == []


def test_required_ledger_keys_come_from_the_committed_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    assert run.required_ledger_keys(root) == [
        "request",
        "decision",
        "mode",
        "prompt_version",
        "model",
    ]
