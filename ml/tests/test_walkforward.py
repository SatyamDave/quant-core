import shutil
from pathlib import Path

import pytest

from ml.validation import walkforward as wf
from ml.validation.walkforward import registration, verdict_of
from registry import Trial, record, trial_count


def test_a_gate_not_evaluated_blocks_pass() -> None:
    assert verdict_of({"a": True, "b": True}) == "PASS"
    assert verdict_of({"a": True, "b": None}) == "FAIL"
    assert verdict_of({"a": True, "b": False}) == "FAIL"


def test_a_failed_walk_forward_registers_a_non_promotable_record() -> None:
    assert registration("PASS") == ("challenger", "")
    assert registration("FAIL") == ("no_signal", "walk-forward FAIL")


def test_scratch_run_deflates_with_the_canonical_trial_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical/trials.jsonl"
    for i in range(6):
        record(canonical, Trial("demo", f"h{i}", False, {"sharpe": 0.01 * i}))
    configs = tmp_path / "backtest/configs"
    configs.mkdir(parents=True)
    shutil.copy(wf.ROOT / "backtest/configs/demo.yaml", configs / "demo.yaml")
    monkeypatch.setattr(wf, "TRIALS", canonical)
    monkeypatch.setattr(wf, "ROOT", tmp_path)
    grid = len(wf.load_config("demo")["model"]["grid_C"])

    report = wf.run("demo").read_text()

    assert f"deflated Sharpe uses {6 + grid} trials for `demo`" in report
    assert "scratch run, not evidence; trials not recorded" in report
    assert trial_count(canonical, "demo") == 6
