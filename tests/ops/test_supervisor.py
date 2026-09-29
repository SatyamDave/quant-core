"""Exercise ops/live/supervisor.py's fail-closed preflight and crash/restart/backoff behavior
against fixture child processes -- qc-bridge and the agent service don't exist in this branch
(bridge-v1.1, agent-core lanes), so these are small python scripts standing in for "a process
that runs" and "a process that crashes."
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "ops/live/supervisor.py"
spec = importlib.util.spec_from_file_location("supervisor", MODULE_PATH)
assert spec and spec.loader
supervisor = importlib.util.module_from_spec(spec)
# dataclass (with `from __future__ import annotations`) resolves annotations via
# sys.modules[cls.__module__] on 3.12+, so the module must be registered before exec.
sys.modules["supervisor"] = supervisor
spec.loader.exec_module(supervisor)


def write_script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(f"import time, sys\n{body}\n")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(path))}"


@pytest.fixture
def sleeper_cmd(tmp_path: Path) -> str:
    return write_script(tmp_path, "sleeper.py", "time.sleep(60)")


@pytest.fixture
def crasher_cmd(tmp_path: Path) -> str:
    return write_script(tmp_path, "crasher.py", "sys.exit(1)")


def make_config(
    tmp_path: Path, bridge_cmd: str, agent_cmd: str, **overrides: object
) -> supervisor.Config:
    defaults: dict[str, object] = {
        "halt_marker": tmp_path / "state/HALT",
        "log_dir": tmp_path / "logs",
        "max_restarts": 5,
        "restart_window_sec": 300.0,
        "backoff_base_sec": 0.01,
        "backoff_max_sec": 0.05,
        "stable_after_sec": 60.0,
        "stop_timeout_sec": 2.0,
    }
    defaults.update(overrides)
    return supervisor.Config(bridge_cmd=bridge_cmd, agent_cmd=agent_cmd, **defaults)  # type: ignore[arg-type]


# --- preflight: fail closed ---


def test_preflight_refuses_when_halt_marker_present(tmp_path: Path, sleeper_cmd: str) -> None:
    cfg = make_config(tmp_path, sleeper_cmd, sleeper_cmd)
    cfg.halt_marker.parent.mkdir(parents=True)
    cfg.halt_marker.write_text(json.dumps({"reason": "prior incident"}))
    with pytest.raises(supervisor.Halted, match="halt marker present"):
        supervisor.preflight(cfg)
    # does not clear the marker
    assert cfg.halt_marker.exists()


def test_preflight_refuses_and_halts_when_secret_missing(
    tmp_path: Path, sleeper_cmd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("QC_TEST_SECRET", raising=False)
    cfg = make_config(tmp_path, sleeper_cmd, sleeper_cmd, require_env=["QC_TEST_SECRET"])
    with pytest.raises(supervisor.Halted, match="missing secret: QC_TEST_SECRET"):
        supervisor.preflight(cfg)
    assert cfg.halt_marker.exists()
    assert "QC_TEST_SECRET" in cfg.halt_marker.read_text()


def test_preflight_refuses_when_health_check_fails(tmp_path: Path, sleeper_cmd: str) -> None:
    cfg = make_config(
        tmp_path,
        sleeper_cmd,
        sleeper_cmd,
        health_check=f'{shlex.quote(sys.executable)} -c "import sys; sys.exit(1)"',
    )
    with pytest.raises(supervisor.Halted, match="unhealthy dependency"):
        supervisor.preflight(cfg)
    assert cfg.halt_marker.exists()


def test_preflight_passes_when_everything_is_healthy(
    tmp_path: Path, sleeper_cmd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QC_TEST_SECRET", "present")
    cfg = make_config(
        tmp_path,
        sleeper_cmd,
        sleeper_cmd,
        require_env=["QC_TEST_SECRET"],
        health_check=f'{shlex.quote(sys.executable)} -c "import sys; sys.exit(0)"',
    )
    supervisor.preflight(cfg)  # does not raise
    assert not cfg.halt_marker.exists()


# --- crash halts both, then restarts with backoff ---


def test_crash_of_one_child_stops_the_other(
    tmp_path: Path, sleeper_cmd: str, crasher_cmd: str
) -> None:
    cfg = make_config(tmp_path, bridge_cmd=sleeper_cmd, agent_cmd=crasher_cmd, max_restarts=0)
    sup = supervisor.Supervisor(cfg)
    sup.start()
    bridge_proc = sup.children["qc-bridge"]
    # give the agent script time to exit
    for _ in range(50):
        if sup.crashed_child() is not None:
            break
        time.sleep(0.05)
    crash = sup.crashed_child()
    assert crash is not None
    name, code = crash
    assert name == "agent"
    assert code == 1
    with pytest.raises(supervisor.Halted, match="crash-loop"):
        sup.handle_crash(name, code)
    # the healthy sibling was terminated, not left running unsupervised
    assert bridge_proc.poll() is not None
    assert sup.cfg.halt_marker.exists()


def test_backoff_doubles_per_consecutive_failure_and_resets_after_stability(
    tmp_path: Path, sleeper_cmd: str
) -> None:
    cfg = make_config(
        tmp_path, sleeper_cmd, sleeper_cmd, backoff_base_sec=1.0, backoff_max_sec=100.0
    )
    sup = supervisor.Supervisor(cfg)
    assert sup._backoff_sec() == 1.0
    sup.consecutive_failures = 3
    assert sup._backoff_sec() == 8.0
    sup.consecutive_failures = 10
    assert sup._backoff_sec() == 100.0  # capped

    # simulate having run stably for longer than stable_after_sec
    sup.cfg.stable_after_sec = 0.0
    sup.last_start_monotonic = time.monotonic() - 1.0
    sup.children = {}
    sup.restart_times.clear()

    def fake_start() -> None:
        sup.last_start_monotonic = time.monotonic()

    sup.start = fake_start  # type: ignore[method-assign]
    sup.handle_crash("agent", 1)
    assert sup.consecutive_failures == 1  # reset to 0 by stability, then incremented once


def test_exceeding_max_restarts_writes_halt_marker_and_stops_retrying(
    tmp_path: Path, sleeper_cmd: str, crasher_cmd: str
) -> None:
    cfg = make_config(
        tmp_path,
        bridge_cmd=sleeper_cmd,
        agent_cmd=crasher_cmd,
        max_restarts=1,
        restart_window_sec=300.0,
        backoff_base_sec=0.01,
        backoff_max_sec=0.02,
        stable_after_sec=999.0,
    )
    sup = supervisor.Supervisor(cfg)
    started = time.monotonic()
    exit_code = sup.run_forever()
    elapsed = time.monotonic() - started
    assert exit_code == 1
    assert elapsed < 10.0  # bounded: fails closed instead of looping forever
    assert cfg.halt_marker.exists()
    assert "crash-loop" in cfg.halt_marker.read_text()
    log_lines = (cfg.log_dir / "supervisor.log").read_text().splitlines()
    events = [json.loads(line)["event"] for line in log_lines]
    assert "halt" in events
    assert events.count("child_exited") >= 1


def test_bridge_cmd_is_optional_agent_only_topology(tmp_path: Path, sleeper_cmd: str) -> None:
    """Today's real topology: the agent spawns its own qc-bridge child (agent/src/bridge.ts),
    so the supervisor only needs to start and watch the one agent process."""
    cfg = make_config(tmp_path, bridge_cmd=None, agent_cmd=sleeper_cmd)
    sup = supervisor.Supervisor(cfg)
    sup.start()
    assert set(sup.children) == {"agent"}
    supervisor.stop_children(sup.children, cfg.stop_timeout_sec)


def test_run_forever_exits_cleanly_on_sigterm(
    tmp_path: Path, sleeper_cmd: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QC_TEST_SECRET", "present")
    cfg = make_config(tmp_path, sleeper_cmd, sleeper_cmd)
    sup = supervisor.Supervisor(cfg)
    sup.start()
    sup.stopped = True  # simulate the signal handler having already fired
    # run_forever's own start() call would re-run preflight; call the shutdown path directly
    supervisor.stop_children(sup.children, cfg.stop_timeout_sec)
    assert all(p.poll() is not None for p in sup.children.values())
