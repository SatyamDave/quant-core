"""Issues #50/#51: scripts/ops/preflight.py refuses to let live trading start unless every
go-live gate passes. One test per gate proving it fails on the bad fixture and passes on the good
one, plus one end-to-end run with every fixture wired to pass at once.

The kill-switch gate (check_kill_switch_test) is exercised here with an injected fake subprocess
runner, never the real `cargo test` -- that real binary is proven elsewhere
(engine/crates/bridge/tests/kill_file_watcher.rs itself, and by hand via `just preflight-live`);
compiling and running it on every `pytest` invocation would make this suite slow for no extra
coverage of this module's own logic.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO / "scripts/ops/preflight.py"
spec = importlib.util.spec_from_file_location("preflight", MODULE_PATH)
assert spec and spec.loader
preflight = importlib.util.module_from_spec(spec)
sys.modules["preflight"] = preflight
spec.loader.exec_module(preflight)

NOW_NS = 1_767_225_600_000_000_000  # 2026-01-01T00:00:00Z, an arbitrary fixed "now"


def completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------------------
# 1. Operator go-live approval
# --------------------------------------------------------------------------------------


def write_approval(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "APPROVED.md"
    path.write_text(body)
    return path


def test_approval_fails_when_missing(tmp_path: Path) -> None:
    result = preflight.check_go_live_approval(tmp_path / "APPROVED.md")
    assert not result.ok
    assert "does not exist" in result.detail


def test_approval_fails_without_an_approved_by_line(tmp_path: Path) -> None:
    result = preflight.check_go_live_approval(write_approval(tmp_path, "Looks good to me.\n"))
    assert not result.ok


def test_approval_fails_when_undated(tmp_path: Path) -> None:
    path = write_approval(tmp_path, "Approved by: Example Operator\n")
    result = preflight.check_go_live_approval(path)
    assert not result.ok
    assert "undated" in result.detail


def test_approval_fails_without_an_operator_name(tmp_path: Path) -> None:
    path = write_approval(tmp_path, "Approved by: 2026-09-29\n")
    result = preflight.check_go_live_approval(path)
    assert not result.ok
    assert "no operator" in result.detail


def test_approval_fails_when_any_line_is_undated(tmp_path: Path) -> None:
    path = write_approval(
        tmp_path, "Approved by: Example Operator 2026-09-29\nApproved by: Second Reviewer\n"
    )
    assert not preflight.check_go_live_approval(path).ok


def test_approval_passes_when_named_and_dated(tmp_path: Path) -> None:
    path = write_approval(tmp_path, "Approved by: Example Operator — 2026-09-29\n")
    result = preflight.check_go_live_approval(path)
    assert result.ok, result.detail


def test_no_approval_is_committed_so_a_fresh_clone_fails_closed() -> None:
    assert not preflight.check_go_live_approval(REPO / preflight.DEFAULT_APPROVAL).ok


# --------------------------------------------------------------------------------------
# 2. Instrument limits within canary caps
# --------------------------------------------------------------------------------------


def write_limits_tree(
    root: Path,
    canary_limits: dict[str, str | int],
    instrument_limits: dict[str, str | int],
) -> Path:
    (root / "config/limits").mkdir(parents=True, exist_ok=True)
    (root / "config/environments").mkdir(parents=True, exist_ok=True)
    (root / "config/instruments").mkdir(parents=True, exist_ok=True)

    def dump(d: dict[str, str | int]) -> str:
        lines = []
        for k, v in d.items():
            lines.append(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}")
        return "\n".join(lines) + "\n"

    (root / "config/limits/default.toml").write_text(dump(canary_limits))
    (root / "config/limits/instrument.toml").write_text(dump(instrument_limits))
    (root / "config/environments/canary.toml").write_text('limits = "config/limits/default.toml"\n')
    instrument_path = root / "config/instruments/instrument.toml"
    instrument_path.write_text('limits = "../limits/instrument.toml"\n')
    return instrument_path


def test_instrument_limits_fails_when_instrument_exceeds_canary_cap(tmp_path: Path) -> None:
    instrument_path = write_limits_tree(
        tmp_path,
        canary_limits={"max_notional": "500", "max_daily_notional": "2000"},
        instrument_limits={"max_notional": "1000", "max_daily_notional": "2000"},
    )
    result = preflight.check_instrument_limits(tmp_path, instrument_path)
    assert not result.ok
    assert "max_notional" in result.detail


def test_instrument_limits_ignores_unit_dependent_max_position(tmp_path: Path) -> None:
    """The real repo's config/limits/spy.toml (10 whole shares) vs default.toml (0.01 BTC) is not
    a real violation -- different units for different asset classes. Reproduced narrowly here."""
    instrument_path = write_limits_tree(
        tmp_path,
        canary_limits={
            "max_position": "0.01",
            "max_notional": "500",
            "max_daily_notional": "2000",
        },
        instrument_limits={
            "max_position": "10",
            "max_notional": "500",
            "max_daily_notional": "2000",
        },
    )
    result = preflight.check_instrument_limits(tmp_path, instrument_path)
    assert result.ok


def test_instrument_limits_fails_when_instrument_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "config/instruments/missing.toml"
    result = preflight.check_instrument_limits(tmp_path, missing)
    assert not result.ok
    assert "does not exist" in result.detail


def test_instrument_limits_passes_when_within_caps(tmp_path: Path) -> None:
    caps = {"max_notional": "500", "max_daily_notional": "2000", "max_order_rate_per_sec": 5}
    instrument_path = write_limits_tree(tmp_path, canary_limits=caps, instrument_limits=dict(caps))
    result = preflight.check_instrument_limits(tmp_path, instrument_path)
    assert result.ok


def test_real_spy_limits_fit_the_committed_canary_caps() -> None:
    """The committed spy limits pass the committed canary caps ($25/order, $50/day)."""
    result = preflight.check_instrument_limits(REPO, REPO / "config/instruments/spy.toml")
    assert result.ok, result.detail
    canary = preflight.load_toml(REPO / "config/limits/default.toml")
    assert canary["max_notional"] == "25"
    assert canary["max_daily_notional"] == "50"


def test_a_26_dollar_order_cap_fails_the_real_canary_caps(tmp_path: Path) -> None:
    (tmp_path / "limits.toml").write_text(
        (REPO / "config/limits/spy.toml")
        .read_text()
        .replace('max_notional = "25"', 'max_notional = "26"')
    )
    instrument = tmp_path / "instrument.toml"
    instrument.write_text('limits = "limits.toml"\n')
    result = preflight.check_instrument_limits(REPO, instrument)
    assert not result.ok
    assert "max_notional: instrument=26 > canary cap=25" in result.detail


def test_instrument_limits_fails_closed_on_malformed_toml(tmp_path: Path) -> None:
    instrument_path = tmp_path / "config/instruments/instrument.toml"
    instrument_path.parent.mkdir(parents=True)
    instrument_path.write_text("not = [valid toml")
    result = preflight._gate(
        "instrument_limits_within_canary",
        lambda: preflight.check_instrument_limits(tmp_path, instrument_path),
    )
    assert not result.ok


# --------------------------------------------------------------------------------------
# 3. autonomy/POLICY.yaml loop-agent-eval
# --------------------------------------------------------------------------------------


def write_policy(root: Path, loop_agent_eval: str) -> Path:
    # classify.load_policy requires every tier in TIER_ORDER (T0-T3) to have a "human_approvals"
    # key, not just whichever tier this fixture actually uses -- match that shape.
    path = root / "POLICY.yaml"
    tier = '{"human_approvals": 1, "agent_reviews": [], "auto_merge_enabled": false}'
    path.write_text(
        "{\n"
        '  "version": 1,\n'
        '  "default_tier": "T2",\n'
        f'  "tiers": {{"T0": {tier}, "T1": {tier}, "T2": {tier}, "T3": {tier}}},\n'
        '  "rules": [],\n'
        '  "label_tiers": {},\n'
        '  "loops": {\n'
        f"    {loop_agent_eval}\n"
        "  }\n"
        "}\n"
    )
    return path


def test_policy_fails_when_disabled(tmp_path: Path) -> None:
    path = write_policy(tmp_path, '"loop-agent-eval": {"enabled": false, "daily_usd": 0}')
    result = preflight.check_policy_agent_eval(path)
    assert not result.ok
    assert "enabled is false" in result.detail


def test_policy_fails_when_enabled_with_zero_budget(tmp_path: Path) -> None:
    path = write_policy(tmp_path, '"loop-agent-eval": {"enabled": true, "daily_usd": 0}')
    result = preflight.check_policy_agent_eval(path)
    assert not result.ok
    assert "daily_usd" in result.detail


def test_policy_fails_when_missing_entry(tmp_path: Path) -> None:
    path = write_policy(tmp_path, '"loop-other": {"enabled": true, "daily_usd": 5}')
    result = preflight.check_policy_agent_eval(path)
    assert not result.ok
    assert "no loops.loop-agent-eval" in result.detail


def test_policy_passes_when_enabled_with_positive_budget(tmp_path: Path) -> None:
    path = write_policy(tmp_path, '"loop-agent-eval": {"enabled": true, "daily_usd": 5}')
    result = preflight.check_policy_agent_eval(path)
    assert result.ok


def test_policy_fails_closed_on_missing_file(tmp_path: Path) -> None:
    result = preflight.check_policy_agent_eval(tmp_path / "does-not-exist.yaml")
    assert not result.ok


# --------------------------------------------------------------------------------------
# 4. Kill-file configured
# --------------------------------------------------------------------------------------


def test_kill_file_configured_fails_when_key_missing(tmp_path: Path) -> None:
    env_dir = tmp_path / "config/environments"
    env_dir.mkdir(parents=True)
    (env_dir / "canary.toml").write_text('name = "canary"\n')
    result = preflight.check_kill_file_configured(tmp_path)
    assert not result.ok


def test_kill_file_configured_passes_when_set(tmp_path: Path) -> None:
    env_dir = tmp_path / "config/environments"
    env_dir.mkdir(parents=True)
    (env_dir / "canary.toml").write_text('kill_file = "ops/live/state/KILL"\n')
    result = preflight.check_kill_file_configured(tmp_path)
    assert result.ok


# --------------------------------------------------------------------------------------
# 5. Kill-switch test (real-binary integration is exercised elsewhere; this checks the
#    gate's own pass/fail wiring against an injected runner)
# --------------------------------------------------------------------------------------


def test_kill_switch_test_fails_on_nonzero_exit(tmp_path: Path) -> None:
    result = preflight.check_kill_switch_test(
        tmp_path, runner=lambda *a, **k: completed(1, stderr="assertion failed")
    )
    assert not result.ok
    assert "assertion failed" in result.detail


def test_kill_switch_test_passes_on_zero_exit(tmp_path: Path) -> None:
    result = preflight.check_kill_switch_test(
        tmp_path, runner=lambda *a, **k: completed(0, stdout="test result: ok. 1 passed")
    )
    assert result.ok


# --------------------------------------------------------------------------------------
# 6. Reconciliation freshness
# --------------------------------------------------------------------------------------


def test_reconcile_fails_when_missing(tmp_path: Path) -> None:
    result = preflight.check_reconcile_recent(tmp_path, Path("reconcile.json"), NOW_NS, 60.0)
    assert not result.ok


def test_reconcile_fails_when_dirty(tmp_path: Path) -> None:
    path = tmp_path / "reconcile.json"
    payload = {"ts_ns": NOW_NS, "ok": False, "discrepancies": ["position mismatch"]}
    path.write_text(json.dumps(payload))
    result = preflight.check_reconcile_recent(tmp_path, path, NOW_NS, 60.0)
    assert not result.ok
    assert "position mismatch" in result.detail


def test_reconcile_fails_when_stale(tmp_path: Path) -> None:
    stale_ts = NOW_NS - int(120 * preflight.MINUTE_NS)
    path = tmp_path / "reconcile.json"
    path.write_text(json.dumps({"ts_ns": stale_ts, "ok": True}))
    result = preflight.check_reconcile_recent(tmp_path, path, NOW_NS, 60.0)
    assert not result.ok
    assert "older than" in result.detail


def test_reconcile_passes_when_clean_and_recent(tmp_path: Path) -> None:
    recent_ts = NOW_NS - int(5 * preflight.MINUTE_NS)
    path = tmp_path / "reconcile.json"
    path.write_text(json.dumps({"ts_ns": recent_ts, "ok": True}))
    result = preflight.check_reconcile_recent(tmp_path, path, NOW_NS, 60.0)
    assert result.ok


# --------------------------------------------------------------------------------------
# 7. Required secret names present
# --------------------------------------------------------------------------------------


def test_secrets_fails_when_missing() -> None:
    result = preflight.check_secrets_present({"OTHER": "x"})
    assert not result.ok
    assert "ANTHROPIC_API_KEY" in result.detail
    assert "x" not in result.detail  # never the value, even accidentally


def test_secrets_passes_when_present() -> None:
    result = preflight.check_secrets_present(
        {"ANTHROPIC_API_KEY": "y", "QC_BROKER_MODULE": "adapters/my-broker.ts"}
    )
    assert result.ok


def test_secrets_require_a_broker_adapter_module() -> None:
    result = preflight.check_secrets_present({"ANTHROPIC_API_KEY": "y"})
    assert not result.ok
    assert "QC_BROKER_MODULE" in result.detail


def test_openrouter_decider_also_requires_the_broker_adapter_module() -> None:
    assert "QC_BROKER_MODULE" in preflight.DECIDERS["openrouter"][1]


# --------------------------------------------------------------------------------------
# 8. Latency p99 under the approval TTL
# --------------------------------------------------------------------------------------


def write_latency_results(tmp_path: Path, ttl_seconds: float, p99_ms: float) -> Path:
    path = tmp_path / "latency-results.json"
    path.write_text(
        json.dumps(
            {
                "approval_ttl_seconds": ttl_seconds,
                "components": {"fake_full_loop": {"p99_ms": p99_ms}},
            }
        )
    )
    return path


def test_latency_fails_when_missing(tmp_path: Path) -> None:
    result = preflight.check_latency_under_ttl(tmp_path, Path("latency-results.json"))
    assert not result.ok


def test_latency_fails_when_over_ttl(tmp_path: Path) -> None:
    path = write_latency_results(tmp_path, ttl_seconds=5.0, p99_ms=6000.0)
    result = preflight.check_latency_under_ttl(tmp_path, path)
    assert not result.ok


def test_latency_passes_when_under_ttl(tmp_path: Path) -> None:
    path = write_latency_results(tmp_path, ttl_seconds=5.0, p99_ms=0.28)
    result = preflight.check_latency_under_ttl(tmp_path, path)
    assert result.ok


# --------------------------------------------------------------------------------------
# End-to-end: every gate wired to a passing fixture at once
# --------------------------------------------------------------------------------------


def test_run_all_gates_all_pass_with_fixtures(tmp_path: Path) -> None:
    approval_path = write_approval(tmp_path, "Approved by: Example Operator 2026-09-29\n")

    instrument_path = write_limits_tree(
        tmp_path,
        canary_limits={"max_notional": "500", "max_daily_notional": "2000"},
        instrument_limits={"max_notional": "500", "max_daily_notional": "2000"},
    )
    # write_limits_tree already wrote a bare canary.toml; add the other required keys.
    canary_path = tmp_path / "config/environments/canary.toml"
    canary_path.write_text(canary_path.read_text() + 'kill_file = "ops/live/state/KILL"\n')

    policy_path = write_policy(tmp_path, '"loop-agent-eval": {"enabled": true, "daily_usd": 5}')

    reconcile_path = tmp_path / "ops/live/state/reconcile-status.json"
    reconcile_path.parent.mkdir(parents=True)
    reconcile_path.write_text(
        json.dumps({"ts_ns": NOW_NS - int(5 * preflight.MINUTE_NS), "ok": True})
    )

    latency_path = write_latency_results(tmp_path, ttl_seconds=5.0, p99_ms=0.28)

    results = preflight.run_all_gates(
        root=tmp_path,
        instrument_path=instrument_path,
        approval_path=approval_path,
        policy_path=policy_path,
        reconcile_status=reconcile_path,
        latency_results=latency_path,
        max_reconcile_age_min=60.0,
        now_ns=NOW_NS,
        env={"ANTHROPIC_API_KEY": "y", "QC_BROKER_MODULE": "adapters/my-broker.ts"},
        kill_test_runner=lambda *a, **k: completed(0, stdout="test result: ok. 1 passed"),
    )

    failed = [r for r in results if not r.ok]
    assert not failed, failed
    assert len(results) == 8


def test_run_all_gates_all_fail_on_empty_root(tmp_path: Path) -> None:
    results = preflight.run_all_gates(
        root=tmp_path,
        instrument_path=tmp_path / "config/instruments/spy.toml",
        approval_path=tmp_path / "config/go-live/APPROVED.md",
        policy_path=tmp_path / "autonomy/POLICY.yaml",
        reconcile_status=tmp_path / "ops/live/state/reconcile-status.json",
        latency_results=tmp_path / "out/bench/latency-results.json",
        max_reconcile_age_min=60.0,
        now_ns=NOW_NS,
        env={},
        kill_test_runner=lambda *a, **k: completed(1, stderr="qc-bridge: not found"),
    )
    assert all(not r.ok for r in results)


def test_main_exits_nonzero_and_prints_every_failing_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = preflight.main(
        [
            "--root",
            str(tmp_path),
            "--now-ns",
            str(NOW_NS),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert out.count("FAIL ") >= 7
    assert "Not clear to go live" in out
