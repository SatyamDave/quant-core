"""scripts/ops/live_canary.py: refuses unless every preflight gate passes, and wires the live
children (recorder, supervisor -> agent -> qc-bridge external) the way the runbook says.

Never starts a real child: `start` is injected. The chain itself runs against mocks in
agent/tests/canary-chain.test.ts (`just live-canary SYMBOL --mock`).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("live_canary", REPO / "scripts/ops/live_canary.py")
assert spec and spec.loader
lc = importlib.util.module_from_spec(spec)
sys.modules["live_canary"] = lc
spec.loader.exec_module(lc)


def ok_runner(*_a: object, **_k: object) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, "test result: ok", "")


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.instrument, self.env = lc.write_mock_fixtures(root, "F")
        self.runner: Callable[..., subprocess.CompletedProcess] = ok_runner
        self.started: list[str] = []

    def run(self, argv: list[str] | None = None) -> int:
        def start(instrument: object, _args: object) -> int:
            self.started.append(instrument.symbol)  # type: ignore[attr-defined]
            return 0

        return lc.main(
            argv or ["F"],
            root=self.root,
            env=self.env,
            now_ns=lc.MOCK_NOW_NS,
            kill_test_runner=self.runner,
            start=start,
        )


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_starts_when_every_gate_passes(h: Harness) -> None:
    assert h.run() == 0
    assert h.started == ["F"]


def _missing_approval(h: Harness) -> None:
    (h.root / "config/go-live/APPROVED.md").unlink()


def _undated_approval(h: Harness) -> None:
    (h.root / "config/go-live/APPROVED.md").write_text("Approved by: Example Operator\n")


def _loose_limits(h: Harness) -> None:
    path = h.root / "config/limits/spy.toml"
    path.write_text(path.read_text().replace('max_notional = "25"', 'max_notional = "1000"'))


def _policy_disabled(h: Harness) -> None:
    path = h.root / "autonomy/POLICY.yaml"
    path.write_text(
        path.read_text().replace(
            '"loop-agent-openrouter": {"enabled": true',
            '"loop-agent-openrouter": {"enabled": false',
        )
    )


def _no_kill_file(h: Harness) -> None:
    path = h.root / "config/environments/canary.toml"
    path.write_text(path.read_text().replace('kill_file = "ops/live/state/KILL"', ""))


def _kill_test_fails(h: Harness) -> None:
    h.runner = lambda *_a, **_k: subprocess.CompletedProcess([], 101, "", "test failed")


def _dirty_reconcile(h: Harness) -> None:
    (h.root / "ops/live/state/reconcile-status.json").write_text(
        json.dumps({"ts_ns": lc.MOCK_NOW_NS, "ok": False})
    )


def _missing_key(h: Harness) -> None:
    del h.env["OPENROUTER_API_KEY"]


def _no_broker_adapter(h: Harness) -> None:
    del h.env["QC_BROKER_MODULE"]


def _slow_latency(h: Harness) -> None:
    (h.root / "out/bench/latency-results.json").write_text(
        json.dumps({"approval_ttl_seconds": 5, "components": {"fake_full_loop": {"p99_ms": 9000}}})
    )


@pytest.mark.parametrize(
    "break_gate",
    [
        _missing_approval,
        _undated_approval,
        _loose_limits,
        _policy_disabled,
        _no_kill_file,
        _kill_test_fails,
        _dirty_reconcile,
        _missing_key,
        _no_broker_adapter,
        _slow_latency,
    ],
)
def test_refuses_and_starts_nothing_when_any_gate_fails(
    h: Harness, break_gate: Callable[[Harness], None], capsys: pytest.CaptureFixture[str]
) -> None:
    break_gate(h)
    assert h.run() == 1
    assert h.started == []
    captured = capsys.readouterr()
    assert "FAIL " in captured.out
    assert "preflight gate(s) failed" in captured.err


def test_the_claude_gate_alone_does_not_clear_an_openrouter_canary(h: Harness) -> None:
    path = h.root / "autonomy/POLICY.yaml"
    text = path.read_text()
    text = text.replace(
        '"loop-agent-openrouter": {"enabled": true', '"loop-agent-openrouter": {"enabled": false'
    )
    text = text.replace(
        '"loop-agent-eval": {"enabled": false, "daily_usd": 0',
        '"loop-agent-eval": {"enabled": true, "daily_usd": 5',
    )
    path.write_text(text)
    assert h.run() == 1
    assert h.started == []


@pytest.mark.parametrize("marker", ["ops/live/state/KILL", "ops/live/state/HALT"])
def test_refuses_while_a_kill_or_halt_marker_exists(h: Harness, marker: str) -> None:
    (h.root / marker).write_text("")
    assert h.run() == 1
    assert h.started == []


def test_refuses_a_malformed_symbol_or_one_without_an_instrument_file(h: Harness) -> None:
    assert h.run(["f"]) == 1
    assert h.run(["XYZ"]) == 1  # no config/instruments/xyz.toml in the fixture tree
    assert h.started == []


def test_live_children_are_wired_for_external_venue_openrouter_and_the_kill_file(
    tmp_path: Path,
) -> None:
    instrument = lc.shadow.load_instrument("SPY")
    kill = tmp_path / "KILL"
    specs = lc.build_specs(
        instrument, tmp_path / "qc-bridge", tmp_path / "rec.csv", tmp_path, kill, 23_400, {}
    )
    recorder, supervisor = specs
    assert "--dry-run" not in recorder.cmd
    assert recorder.cmd[recorder.cmd.index("--instrument-id") + 1] == "2"
    assert recorder.cmd[recorder.cmd.index("--out") + 1] == str(tmp_path / "rec.csv")
    bridge = supervisor.env["QC_BRIDGE_ARGS"].split()
    assert bridge[0] == str(tmp_path / "rec.csv")
    for flag, value in (
        ("--venue", "external"),
        ("--instrument", str(instrument.path)),
        ("--kill-file", str(kill)),
        ("--decide-every", "900"),
    ):
        assert bridge[bridge.index(flag) + 1] == value
    assert "--follow" in bridge
    assert supervisor.env["QC_AGENT_MODE"] == "openrouter"
    assert supervisor.env["QC_INSTRUMENT"] == "SPY"
    cmd = supervisor.cmd
    assert cmd[cmd.index("--max-restarts") + 1] == "0"
    required = {cmd[i + 1] for i, a in enumerate(cmd) if a == "--require-env"}
    assert required == set(lc.REQUIRED_ENV)


def test_a_path_with_a_space_is_refused(tmp_path: Path) -> None:
    instrument = lc.shadow.load_instrument("SPY")
    with pytest.raises(ValueError, match="space"):
        lc.build_specs(instrument, tmp_path / "q b", tmp_path / "r.csv", tmp_path, tmp_path, 1, {})


def _ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(dt.datetime(year, month, day, hour, minute, tzinfo=lc.ET).timestamp() * 1e9)


def test_session_over_waits_for_the_open_and_stops_at_the_close() -> None:
    spy = lc.shadow.load_instrument("SPY").path
    now = [_ns(2026, 9, 29, 9, 25)]
    over = lc.session_over_check(spy, clock=lambda: now[0])
    assert not over()  # before the open: wait
    now[0] = _ns(2026, 9, 29, 12, 0)
    assert not over()
    now[0] = _ns(2026, 9, 29, 16, 0)
    assert over()


def test_session_over_immediately_after_the_close() -> None:
    spy = lc.shadow.load_instrument("SPY").path
    over = lc.session_over_check(spy, clock=lambda: _ns(2026, 9, 29, 17, 0))
    assert over()


def test_reconcile_only_cannot_trade(tmp_path: Path) -> None:
    instrument = lc.shadow.load_instrument("SPY")
    cmd, env = lc.reconcile_only_cmd(
        instrument, tmp_path / "qc-bridge", tmp_path / "e.csv", tmp_path / "K"
    )
    assert cmd[-1] == "src/cli.ts"
    assert env["QC_AGENT_MODE"] == "fake"
    bridge = env["QC_BRIDGE_ARGS"].split()
    assert bridge[0] == str(tmp_path / "e.csv")  # an empty, finished recording: zero decisions
    assert "--follow" not in bridge
    assert bridge[bridge.index("--venue") + 1] == "external"


def test_reconcile_only_needs_the_broker_adapter(h: Harness) -> None:
    del h.env["QC_BROKER_MODULE"]
    assert h.run(["F", "--reconcile-only"]) == 1
