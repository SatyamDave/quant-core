"""scripts/eval/live_agent.py: the gate fails closed in this repo (no ANTHROPIC_API_KEY, no
loop-agent-eval policy entry yet), the frozen scenario set exists and is labelled, and the
buy-and-hold baseline's arithmetic (issue #37) is hand-computed and checked without a real
qc-bridge session — see scripts/eval/bridge_sessions.py / `just eval`'s
risk_invariants_bridge_sessions case for the real-session-level verification that isn't
appropriate to re-run in this fast, no-subprocess suite."""

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("live_agent", ROOT / "scripts/eval/live_agent.py")
assert SPEC and SPEC.loader
live_agent = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = live_agent
SPEC.loader.exec_module(live_agent)

D = Decimal


def test_gate_denies_today() -> None:
    # autonomy/POLICY.yaml.enabled is false and no loop-agent-eval entry exists yet
    # (agentic/agent-service adds it, disabled, zero budget) — this must stay a deny.
    allowed, reason = live_agent.gate_allows(ROOT)
    assert allowed is False
    assert "deny" in reason


def test_main_skips_without_crashing(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert live_agent.main(["--root", str(ROOT)]) == 0


def test_frozen_scenarios_exist_and_are_labelled_synthetic() -> None:
    scenarios = live_agent.scenario_files(ROOT)
    assert len(scenarios) == 4
    for path in scenarios:
        assert "synthetic" in path.read_text().splitlines()[0]


def test_one_scenario_is_labelled_adversarial() -> None:
    scenarios = live_agent.scenario_files(ROOT)
    assert any("adversarial" in path.read_text().splitlines()[0] for path in scenarios)


def test_buy_and_hold_pnl_from_mids_hand_computed() -> None:
    # long 0.01 units entered at 65000, marked at 61822.15: (61822.15-65000)*0.01 = -31.7785
    result = live_agent._buy_and_hold_pnl_from_mids(D("65000"), D("61822.15"), D("0.01"))
    assert D(result) == D("-31.7785")


def test_buy_and_hold_pnl_from_mids_profit_when_price_rises() -> None:
    result = live_agent._buy_and_hold_pnl_from_mids(D("100"), D("110"), D("2"))
    assert D(result) == D("20")  # (110-100)*2
