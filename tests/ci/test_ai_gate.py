"""scripts/ci/ai_gate.py denies every paid AI run unless the policy fully allows it."""

import importlib.util
import json
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[2] / "scripts/ci/ai_gate.py"
spec = importlib.util.spec_from_file_location("ai_gate", GATE)
assert spec and spec.loader
ai_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai_gate)

LOOP = {"enabled": True, "daily_usd": 3, "max_turns": 20, "timeout_minutes": 20}
ON = {"AUTONOMY_ENABLED": "true"}


def write_policy(root: Path, enabled: bool = True, **loop: object) -> None:
    (root / "autonomy").mkdir(exist_ok=True)
    loops = {} if loop.get("missing") else {"loop-x": {**LOOP, **loop}}
    policy = {
        "enabled": enabled,
        "pause_file": "autonomy/PAUSE",
        "pause_repo_variable": "AUTONOMY_ENABLED",
        "loops": loops,
    }
    # Same comment-bearing JSON the real POLICY.yaml uses.
    text = "# policy\n" + json.dumps(policy, indent=1)
    (root / "autonomy/POLICY.yaml").write_text(text)


def run(root: Path, env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    code = ai_gate.main(["loop-x", "--root", str(root)])
    text = out.read_text()
    assert (code == 0) == ("run=true" in text)
    return text


@pytest.mark.parametrize(
    ("policy", "env", "pause"),
    [
        ({"enabled": False}, ON, False),  # global switch off
        ({}, {"AUTONOMY_ENABLED": "false"}, False),  # repo variable off
        ({}, {}, False),  # repo variable unset
        ({"enabled": True, "missing": True}, ON, False),  # no loop entry
        ({"enabled": True}, ON, True),  # PAUSE present
    ],
)
def test_denies_when_switched_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: dict[str, object],
    env: dict[str, str],
    pause: bool,
) -> None:
    monkeypatch.delenv("AUTONOMY_ENABLED", raising=False)
    enabled = bool(policy.pop("enabled", True))
    write_policy(tmp_path, enabled, **policy)
    if pause:
        (tmp_path / "autonomy/PAUSE").write_text("")
    assert run(tmp_path, env, tmp_path, monkeypatch) == "run=false\n"


@pytest.mark.parametrize(
    "loop",
    [
        {"enabled": False},  # loop disabled
        {"daily_usd": None},  # missing budget
        {"daily_usd": 0},  # zero budget
        {"max_turns": 0},
        {"timeout_minutes": -1},
        {"max_turns": True},  # a bool is not a turn count
        {"daily_usd": float("inf")},  # json.dumps writes Infinity, which json.loads accepts
        {"max_turns": float("nan")},  # NaN <= 0 is false, so a sign check alone lets it through
    ],
)
def test_denies_without_enabled_loop_and_positive_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loop: dict[str, object]
) -> None:
    write_policy(tmp_path, **loop)
    assert run(tmp_path, ON, tmp_path, monkeypatch) == "run=false\n"


def test_missing_budget_key_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_policy(tmp_path)
    policy = (tmp_path / "autonomy/POLICY.yaml").read_text().replace('"daily_usd": 3,', "")
    (tmp_path / "autonomy/POLICY.yaml").write_text(policy)
    assert run(tmp_path, ON, tmp_path, monkeypatch) == "run=false\n"


def test_unreadable_policy_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(tmp_path, ON, tmp_path, monkeypatch) == "run=false\n"


def test_allows_and_passes_the_loop_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_policy(tmp_path)
    assert run(tmp_path, ON, tmp_path, monkeypatch) == (
        "run=true\nmax_turns=20\nmax_budget_usd=3\ntimeout_minutes=20\n"
    )


def test_real_policy_denies_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTONOMY_ENABLED", "true")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert ai_gate.main(["loop-knowledge"]) == 1
