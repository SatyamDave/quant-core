"""Feeds the *real* qc-bridge binary's stderr into scripts/ops/alerts.py and checks the expected
alerts fire (wave-2 gap list: "structured stderr event log matching scripts/ops/alerts.py's
contract" -- prove it against real bridge output, not a hand-written fixture).

engine/crates/bridge/src/eventlog.rs is the one place this log format is produced; this test is
the other end of that contract, run from the ops side, against a real binary and real risk/OMS
behaviour (a genuine oversized order, a genuine `kill` op) rather than a synthetic JSONL fixture
like tests/ops/fixtures/*.jsonl (those still exist and still test alerts.py's own rules in
isolation; this test is the integration proof that the two sides actually agree).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENGINE = REPO / "engine"
ALERTS_MODULE_PATH = REPO / "scripts/ops/alerts.py"

spec = importlib.util.spec_from_file_location("alerts", ALERTS_MODULE_PATH)
assert spec and spec.loader
alerts = importlib.util.module_from_spec(spec)
sys.modules["alerts"] = alerts
spec.loader.exec_module(alerts)

RECORDING = "S,1,10,1000,1000,100.0@1;99.9@1,100.2@1;100.3@1\nD,1,11,B,99.8,1,2000,2000\n"

# Two risk-reject-worthy submits (qty 1000 blows through every limit in
# config/limits/default.toml, e.g. max_notional=500) plus one `kill` (protocol
# v1.2, issue #44) -- exercises risk_reject, kill_switch_engaged and halt in one script.
SCRIPT = "\n".join(
    [
        '{"v":1,"id":"dr","op":"next_decision_request"}',
        '{"v":1,"id":"s1","op":"submit_order_intent","intent":'
        '{"request_id":"dr-1","instrument":"1","side":"buy","qty":"1000",'
        '"limit_price":"99.8","time_in_force":"gtc","reason":"test"}}',
        '{"v":1,"id":"k","op":"kill","reason":"chaos test trigger"}',
        '{"v":1,"id":"end","op":"shutdown"}',
        "",
    ]
)


@pytest.fixture(scope="module")
def bridge_binary() -> Path:
    subprocess.run(
        ["cargo", "build", "--locked", "-q", "-p", "qc-bridge"],  # noqa: S607 - PATH lookup, like `just`'s own recipes
        cwd=ENGINE,
        check=True,
    )
    binary = ENGINE / "target/debug/qc-bridge"
    assert binary.is_file(), "cargo build must produce engine/target/debug/qc-bridge"
    return binary


@pytest.fixture(scope="module")
def bridge_stderr(bridge_binary: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("bridge-alerts-contract")
    recording = tmp_path / "recording.csv"
    recording.write_text(RECORDING)
    result = subprocess.run(  # noqa: S603 - fixed argv built from this test's own paths, not external input
        [str(bridge_binary), str(recording), "--limits", str(REPO / "config/limits/default.toml")],
        input=SCRIPT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    log_path = tmp_path / "qc-bridge.log"
    log_path.write_text(result.stderr)
    return log_path


def test_bridge_emits_at_least_one_json_line_per_expected_event(bridge_stderr: Path) -> None:
    events = alerts.read_jsonl(bridge_stderr)
    assert events, "the bridge must write structured lines to stderr, not nothing"
    kinds = {e.get("event") for e in events}
    assert {"risk_reject", "kill_switch_engaged", "halt"} <= kinds, events


def test_real_bridge_stderr_drives_the_halt_and_kill_switch_alerts(
    bridge_stderr: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = alerts.main(
        ["--bridge-log", str(bridge_stderr), "--state-file", str(tmp_path / "alerts-seen.json")]
    )
    out = capsys.readouterr().out
    assert code == 1, out
    assert "halt" in out
    assert "kill_switch:" in out
    assert alerts.RUNBOOKS["kill_switch"] in out


def test_real_bridge_stderr_lines_all_parse_and_carry_ts_ns(bridge_stderr: Path) -> None:
    for event in alerts.read_jsonl(bridge_stderr):
        assert isinstance(event.get("ts_ns"), int), event
        assert isinstance(event.get("event"), str), event
