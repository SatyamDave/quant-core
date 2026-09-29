#!/usr/bin/env python3
"""Risk-invariant regressions asserted via real `qc-bridge` sessions (issue #38): known-bad
inputs sent as JSON Lines to the actual compiled binary over stdin/stdout — a true black-box
check across the process boundary, not a call into the Rust crate's own unit tests. Kept in
scripts/eval/ (this lane's path) rather than engine/crates/bridge/tests/ (a different lane's
protected path); see case_risk_invariants in run.py for how this is wired into `just eval`.

Each case is deliberately a *regression* the way root CLAUDE.md rule 4 frames it: a case here must
fail if a limit were silently loosened. `stale_data_boundary_control` proves that directly, by
running the identical scenario against a deliberately-loosened `stale_data_ms` and asserting the
order is then accepted — i.e., this suite would have caught that loosening.

Standard library only, matching every other script in scripts/eval and scripts/ci.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_BIN = ROOT / "engine/target/release/qc-bridge"
DEFAULT_LIMITS = ROOT / "config/limits/default.toml"

# One synced instrument-1 snapshot (bid 100.0 / ask 100.2, tick 0.1), then a single trade far in
# the future (+5s of local time) that never touches the book. Feeding this trade after the
# snapshot lets the book go stale (book.last_update stays at the snapshot's timestamp) while the
# bridge's clock still advances, without ever producing a second decide-ready state — the
# `--decide-every 1` snapshot already satisfied that, and a lone trade record touches neither the
# book nor the decide-every counter (engine/crates/bridge/src/engine.rs process_record).
STALE_SCENARIO_CSV = (
    "S,1,1,1000,1000,100.0@1;99.9@1,100.2@1;100.3@1\nT,1,B,100.1,1,5000000000,5000000000\n"
)

VALID_ORDER = {
    "request_id": "r1",
    "instrument": "1",
    "side": "buy",
    "qty": "0.0001",
    "limit_price": "100.1",
    "time_in_force": "ioc",
    "reason": "eval: risk-invariant regression",
}


def ensure_bridge_built(root: Path = ROOT) -> tuple[bool, str]:
    proc = subprocess.run(
        ["cargo", "build", "--release", "--locked", "-q", "-p", "qc-bridge"],  # noqa: S607
        cwd=root / "engine",
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        return (
            False,
            f"cargo build -p qc-bridge exited {proc.returncode}\n{proc.stdout}{proc.stderr}",
        )
    return True, "built"


def run_session(
    recording_csv: str,
    requests: list[dict[str, Any]],
    limits_path: Path = DEFAULT_LIMITS,
    decide_every: int = 1,
    root: Path = ROOT,
) -> list[dict[str, Any]]:
    """Feeds `requests` as JSON Lines to a fresh `qc-bridge` process over the given recording and
    returns the parsed JSON Lines it wrote back, in order."""
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
        f.write(recording_csv)
        recording_path = Path(f.name)
    try:
        stdin_text = "\n".join(json.dumps(r) for r in requests) + "\n"
        proc = subprocess.run(  # noqa: S603 - fixed local binary, argv built from literals above
            [
                str(root / "engine/target/release/qc-bridge"),
                str(recording_path),
                "--limits",
                str(limits_path),
                "--decide-every",
                str(decide_every),
            ],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        recording_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"qc-bridge exited {proc.returncode}\n{proc.stdout}{proc.stderr}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _req(op: str, req_id: str, **extra: Any) -> dict[str, Any]:
    return {"v": 1, "id": req_id, "op": op, **extra}


def oversized_order_rejected(root: Path = ROOT) -> tuple[bool, str]:
    """1000 units at ~99.8 blows through max_notional (25); 0.02 units at the same price (~$2)
    stays under notional but blows through max_position (0.01) — the same two cases
    engine/crates/bridge/tests/risk_rejection.rs asserts in-process, run here as a black-box
    session against the compiled binary instead."""
    responses = run_session(
        STALE_SCENARIO_CSV,
        [
            _req("next_decision_request", "1"),
            _req(
                "submit_order_intent",
                "2",
                intent={**VALID_ORDER, "qty": "1000", "limit_price": "99.8"},
            ),
            _req(
                "submit_order_intent",
                "3",
                intent={**VALID_ORDER, "qty": "0.02", "limit_price": "99.8"},
            ),
        ],
    )
    notional_result = responses[1]["result"]
    position_result = responses[2]["result"]
    if notional_result != {"accepted": False, "risk_reject": "max_notional"}:
        return False, f"oversized notional: expected max_notional reject, got {notional_result}"
    if position_result != {"accepted": False, "risk_reject": "max_position"}:
        return False, f"oversized position: expected max_position reject, got {position_result}"
    return True, "1000@99.8 rejected max_notional; 0.02@99.8 rejected max_position"


def order_rate_burst_rejected(root: Path = ROOT) -> tuple[bool, str]:
    """Six well-formed orders inside the same one-second window; `max_order_rate_per_sec` (5, the
    committed default) accepts the first five and rejects the sixth on rate alone (qty small
    enough that notional/position never bind first)."""
    requests = [_req("next_decision_request", "0")]
    for i in range(6):
        requests.append(
            _req(
                "submit_order_intent",
                str(i + 1),
                intent={**VALID_ORDER, "request_id": f"r{i}", "qty": "0.0001"},
            )
        )
    responses = run_session(STALE_SCENARIO_CSV, requests)
    results = [r["result"] for r in responses[1:]]
    accepted = [r["accepted"] for r in results]
    if accepted != [True, True, True, True, True, False]:
        return False, f"expected 5 accepts then 1 reject, got {results}"
    if results[5].get("risk_reject") != "max_order_rate":
        return False, f"6th order should be rejected max_order_rate, got {results[5]}"
    return True, "5 orders accepted inside the window, 6th rejected max_order_rate"


def _stale_data_case(limits_path: Path) -> dict[str, Any]:
    responses = run_session(
        STALE_SCENARIO_CSV,
        [
            _req("next_decision_request", "1"),
            _req("next_decision_request", "2"),  # exhausts the recording; clock advances 5s
            _req("submit_order_intent", "3", intent=VALID_ORDER),
        ],
        limits_path=limits_path,
    )
    return responses[2]["result"]


def stale_data_rejected(root: Path = ROOT) -> tuple[bool, str]:
    """Book last updates at t=1000ns; the recording's only remaining record is a trade 5s later
    that never touches the book, so by the time the order is submitted the book is ~5s stale
    against the committed `stale_data_ms` (1000ms). Confirms the reject, then reruns the identical
    scenario against a deliberately loosened `stale_data_ms` and confirms the *same* order is then
    accepted — proof this case would fail loudly if the real limit were ever weakened the same
    way (root CLAUDE.md rule 4)."""
    strict = _stale_data_case(DEFAULT_LIMITS)
    if strict != {"accepted": False, "risk_reject": "stale_data"}:
        return False, f"expected stale_data reject under the committed limit, got {strict}"

    original = DEFAULT_LIMITS.read_text()
    loosened_text = original.replace("stale_data_ms = 1000", "stale_data_ms = 999999999999")
    if loosened_text == original:
        return False, "stale_data_ms = 1000 line not found in config/limits/default.toml to loosen"
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(loosened_text)
        loosened_path = Path(f.name)
    try:
        loosened = _stale_data_case(loosened_path)
    finally:
        loosened_path.unlink(missing_ok=True)
    if loosened.get("accepted") is not True:
        return False, (
            "control failed: loosening stale_data_ms should have accepted the same order, "
            f"got {loosened} — this case would not actually catch a real loosening"
        )
    return True, "stale_data rejected at the committed limit; the loosened control accepted it"


CASES = (
    ("oversized_order_rejected", oversized_order_rejected),
    ("order_rate_burst_rejected", order_rate_burst_rejected),
    ("stale_data_rejected", stale_data_rejected),
)


def run_all(root: Path = ROOT) -> list[tuple[str, bool, str]]:
    """Builds the bridge once, then runs every case. Returns (name, ok, detail) triples."""
    built, detail = ensure_bridge_built(root)
    if not built:
        return [(name, False, f"could not build qc-bridge: {detail}") for name, _ in CASES]
    results = []
    for name, fn in CASES:
        try:
            ok, detail = fn(root)
        except Exception as e:  # any failure here is a real, reportable failure
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append((name, ok, detail))
    return results


if __name__ == "__main__":
    import sys

    outcomes = run_all()
    for name, ok, detail in outcomes:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    sys.exit(0 if all(ok for _, ok, _ in outcomes) else 1)
