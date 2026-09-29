"""scripts/shadow/run.py (issue #48, A5 Shadow infrastructure): orchestration logic only.

Deliberately does NOT spawn the real recorder/agent (Node/tsx) or build the real qc-bridge
binary -- no CI job in this repo installs a Node toolchain, and the real follow/determinism
behavior already has its own dedicated coverage in
engine/crates/bridge/tests/follow.rs. What this suite proves is run.py's own code: path
layout, the --live gate's fail-closed wiring against the real scripts/ci/ai_gate.py +
autonomy/POLICY.yaml, the heartbeat/downtime bookkeeping (against small stand-in Python
processes, the same technique tests/ops/test_supervisor.py uses for the same reason), and that
it calls the existing report scripts against its own ledger rather than fund/track-record/daily/.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/shadow/run.py"
spec = importlib.util.spec_from_file_location("shadow_run", MODULE_PATH)
assert spec and spec.loader
shadow_run = importlib.util.module_from_spec(spec)
sys.modules["shadow_run"] = shadow_run
spec.loader.exec_module(shadow_run)

SAMPLE_LEDGER_LINE = (
    '{"request":{"allowed_actions":["buy","sell","no_trade"],"best_ask":"100.05000000",'
    '"best_bid":"100.00000000","features":{},"instrument":"1","limits":{"max_notional":'
    '"500.00000000","max_order_rate_per_sec":5,"max_position":"0.01000000",'
    '"remaining_daily_loss":"100.00000000"},"mid":"100.02500000","position":"0.00000000",'
    '"request_id":"dr-1","signal":null,"spread_ticks":0,"ts_ns":1790635108455000000},'
    '"decision":{"request_id":"dr-1","action":"no_trade","rationale":"no signal"},'
    '"result":null,"mode":"fake","prompt_version":"v1","model":"fake-rule-v1","raw":null}\n'
)


def write_script(tmp_path: Path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(f"import sys, time\n{body}\n")
    return [sys.executable, str(path)]


# --- paths_for -------------------------------------------------------------------------------


def test_paths_never_touch_the_real_track_record_or_catalog(tmp_path: Path) -> None:
    paths = shadow_run.paths_for("2026-09-15", root=tmp_path)
    out_root = tmp_path / "out" / "shadow" / "2026-09-15"
    assert paths.ledger == out_root / "ledger.jsonl"
    assert paths.report_dir.is_relative_to(out_root)
    assert paths.catalog_dir.is_relative_to(out_root)
    # Never the real, committed catalog or the real, append-only broker track record.
    assert paths.catalog_dir != tmp_path / "data" / "catalog"
    assert "track-record" not in str(paths.report_dir)


# --- parse_args -------------------------------------------------------------------------------


def test_dry_run_gets_a_small_fast_default_budget() -> None:
    args = shadow_run.parse_args(["--dry-run"])
    assert args.request_budget == 20
    assert args.decide_every == 3
    assert args.stop_after_idle_ms == 400


def test_a_real_run_refuses_without_an_explicit_request_budget() -> None:
    with pytest.raises(SystemExit):
        shadow_run.parse_args([])  # no --dry-run, no --request-budget


def test_a_real_run_accepts_an_explicit_request_budget_and_follows_forever_by_default() -> None:
    args = shadow_run.parse_args(["--request-budget", "23400"])
    assert args.request_budget == 23400
    assert args.stop_after_idle_ms is None  # never gives up on its own
    assert args.decide_every == 900  # ~one decision per 15 min at 1 s polls, not qc-bridge's 50


def test_the_agent_waits_indefinitely_for_follow_mode_decisions(tmp_path: Path) -> None:
    args = shadow_run.parse_args(["--dry-run"])
    paths = shadow_run.paths_for("2026-01-02", root=tmp_path)
    spy = shadow_run.load_instrument("SPY")
    specs = shadow_run.build_specs(args, paths, "fake", tmp_path / "qc-bridge", spy)
    agent = next(s for s in specs if s.name == "agent")
    assert "--follow" in agent.env["QC_BRIDGE_ARGS"].split()
    assert agent.env["QC_BRIDGE_DECISION_TIMEOUT_MS"] == "0"


def test_the_bridge_gets_the_symbols_instrument_file_and_the_recorder_its_id(
    tmp_path: Path,
) -> None:
    args = shadow_run.parse_args(["--dry-run", "--decider", "openrouter"])
    paths = shadow_run.paths_for("2026-01-02", root=tmp_path)
    spy = shadow_run.load_instrument("SPY")
    specs = shadow_run.build_specs(args, paths, "openrouter", tmp_path / "qc-bridge", spy)
    agent = next(s for s in specs if s.name == "agent")
    recorder = next(s for s in specs if s.name == "recorder")
    bridge_args = agent.env["QC_BRIDGE_ARGS"].split()
    assert bridge_args[bridge_args.index("--instrument") + 1] == str(SPY.resolve())
    assert bridge_args[bridge_args.index("--venue") + 1] == "sim"
    assert "--limits" not in bridge_args  # the instrument file's own limits apply
    assert recorder.cmd[recorder.cmd.index("--instrument-id") + 1] == "2"  # spy.toml's id
    assert agent.env["QC_AGENT_MODE"] == "openrouter"
    assert agent.env["QC_INSTRUMENT"] == "SPY"


def test_a_symbol_without_an_instrument_file_names_the_command_that_makes_one() -> None:
    with pytest.raises(ValueError, match="just new-equity-instrument ZZZZZ"):
        shadow_run.load_instrument("ZZZZZ")


def test_an_instrument_file_for_another_symbol_is_refused() -> None:
    with pytest.raises(ValueError, match="not 'QQQ'"):
        shadow_run.load_instrument("QQQ", SPY)


def test_decider_defaults_to_fake_and_live_is_an_alias() -> None:
    assert shadow_run.parse_args(["--dry-run"]).decider == "fake"
    assert shadow_run.parse_args(["--dry-run", "--live"]).decider == "live"
    with pytest.raises(SystemExit):
        shadow_run.parse_args(["--dry-run", "--live", "--decider", "openrouter"])


# --- check_live_gate ---------------------------------------------------------------------------


def test_live_gate_denies_today_because_loop_agent_eval_is_disabled_zero_budget() -> None:
    # Reads the REAL scripts/ci/ai_gate.py and autonomy/POLICY.yaml (read-only) -- this is
    # exactly the check run.py's --live flag relies on to fail closed; ADR-0040 documents
    # loop-agent-eval as disabled/zero-budget, and this proves that's still true today.
    allowed, reason = shadow_run.check_live_gate(root=ROOT)
    assert allowed is False
    assert "loop-agent-eval" in reason or "POLICY" in reason


def test_main_refuses_openrouter_while_its_gate_is_disabled() -> None:
    exit_code = shadow_run.main(["--dry-run", "--decider", "openrouter", "--date", "2026-09-17"])
    assert exit_code == 1
    assert not shadow_run.paths_for("2026-09-17", root=ROOT).ledger.exists()


def test_main_refuses_live_rather_than_silently_running_fake() -> None:
    exit_code = shadow_run.main(["--dry-run", "--live", "--date", "2026-09-16"])
    assert exit_code == 1
    # No ledger, no recording: the gate must refuse before anything is ever built or spawned.
    paths = shadow_run.paths_for("2026-09-16", root=ROOT)
    assert not paths.ledger.exists()


# --- run_children (heartbeat + downtime bookkeeping) -------------------------------------------


def test_two_clean_children_give_exit_code_zero_and_no_downtime(tmp_path: Path) -> None:
    a = shadow_run.ChildSpec(
        "a", write_script(tmp_path, "a.py", "time.sleep(0.05)"), tmp_path, {}, tmp_path / "a.log"
    )
    b = shadow_run.ChildSpec(
        "b", write_script(tmp_path, "b.py", "time.sleep(0.1)"), tmp_path, {}, tmp_path / "b.log"
    )
    heartbeat = tmp_path / "heartbeat.jsonl"

    result = shadow_run.run_children([a, b], heartbeat, poll_interval_sec=0.02)

    assert result.exit_code == 0
    assert result.downtime == []
    lines = [json.loads(line) for line in heartbeat.read_text().splitlines()]
    assert any(line["event"] == "heartbeat" for line in lines)
    assert lines[-1]["alive"] == {"a": False, "b": False}


def test_one_child_crashing_early_is_documented_as_downtime(tmp_path: Path) -> None:
    crasher = shadow_run.ChildSpec(
        "recorder",
        write_script(tmp_path, "crash.py", "sys.exit(1)"),
        tmp_path,
        {},
        tmp_path / "r.log",
    )
    survivor = shadow_run.ChildSpec(
        "agent",
        write_script(tmp_path, "sleep.py", "time.sleep(0.3)"),
        tmp_path,
        {},
        tmp_path / "a.log",
    )
    heartbeat = tmp_path / "heartbeat.jsonl"

    result = shadow_run.run_children([crasher, survivor], heartbeat, poll_interval_sec=0.02)

    assert result.exit_code == 1
    assert len(result.downtime) == 1
    event = result.downtime[0]
    assert event["name"] == "recorder"
    assert event["exit_code"] == 1
    assert event["still_running"] == ["agent"]
    # The downtime event itself also landed in the heartbeat log, not just the return value.
    lines = [json.loads(line) for line in heartbeat.read_text().splitlines()]
    assert any(line.get("event") == "unexpected_exit" for line in lines)


def test_both_children_exiting_nonzero_together_is_not_reported_as_downtime(tmp_path: Path) -> None:
    # Neither is "still running" when the other fails -- this is a plain failed run, not the
    # "one thing died while the rest kept going" pattern issue #48 asks to document separately.
    a = shadow_run.ChildSpec(
        "a", write_script(tmp_path, "a.py", "sys.exit(1)"), tmp_path, {}, tmp_path / "a.log"
    )
    b = shadow_run.ChildSpec(
        "b", write_script(tmp_path, "b.py", "sys.exit(1)"), tmp_path, {}, tmp_path / "b.log"
    )
    heartbeat = tmp_path / "heartbeat.jsonl"

    result = shadow_run.run_children([a, b], heartbeat, poll_interval_sec=0.02)

    assert result.exit_code == 1
    assert result.downtime == []


# --- recording_stall_watch (#48: a dead recorder must not look like a quiet market) -----------

MON_OPEN_NS = 1790618400 * 1_000_000_000  # 2026-09-28T18:00Z = Mon 14:00 America/New_York
SPY = ROOT / "config/instruments/spy.toml"


def test_stall_watch_reports_a_stall_once_after_the_grace_period(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = [MON_OPEN_NS]
    recording = tmp_path / "2026-09-28.csv"  # never written: the recorder died at startup
    watch = shadow_run.recording_stall_watch(recording, SPY, 120.0, clock_ns=lambda: clock[0])

    assert watch() == []  # recorder startup grace
    clock[0] += 121 * 1_000_000_000
    events = watch()
    assert [e["event"] for e in events] == ["recording_stalled"]
    assert "recording_stalled" in capsys.readouterr().out
    clock[0] += 2 * 1_000_000_000
    assert watch() == []  # still stalled: not re-reported every heartbeat


def test_run_children_logs_heartbeat_events_as_downtime(tmp_path: Path) -> None:
    a = shadow_run.ChildSpec(
        "a", write_script(tmp_path, "a.py", "sys.exit(0)"), tmp_path, {}, tmp_path / "a.log"
    )
    heartbeat = tmp_path / "heartbeat.jsonl"
    event = {"event": "recording_stalled", "message": "m"}

    result = shadow_run.run_children(
        [a], heartbeat, poll_interval_sec=0.02, on_heartbeat=lambda: [event]
    )

    assert event in result.downtime
    assert '"recording_stalled"' in heartbeat.read_text()


# --- write_reports (real scripts/reports/*, no Node/Rust needed) -------------------------------


def test_write_reports_calls_the_real_report_scripts_against_this_runs_own_ledger(
    tmp_path: Path,
) -> None:
    paths = shadow_run.paths_for("2026-09-17", root=tmp_path)
    paths.ledger.parent.mkdir(parents=True, exist_ok=True)
    paths.ledger.write_text(SAMPLE_LEDGER_LINE)

    ok = shadow_run.write_reports(paths, "2026-09-17", root=ROOT)

    assert ok is True
    assert (paths.report_dir / "ledger" / "report.json").exists()
    assert (paths.report_dir / "pnl" / "2026-09-17.json").exists()
    pnl = json.loads((paths.report_dir / "pnl" / "2026-09-17.json").read_text())
    assert pnl["source"] == "decision_ledger_estimate"


def test_write_reports_survives_a_missing_ledger(tmp_path: Path) -> None:
    # A run that produced zero decisions (e.g. --request-budget too small) must still finish
    # cleanly rather than crash the whole script -- ledger_report.py/pnl_report.py both already
    # handle an absent ledger; this proves run.py's own wiring doesn't get in the way of that.
    paths = shadow_run.paths_for("2026-09-18", root=tmp_path)
    ok = shadow_run.write_reports(paths, "2026-09-18", root=ROOT)
    assert ok is True


def teardown_module(_module: object) -> None:
    for date in ("2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"):
        shutil.rmtree(ROOT / "out" / "shadow" / date, ignore_errors=True)
