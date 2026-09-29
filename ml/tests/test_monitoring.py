from pathlib import Path

import numpy as np
import pytest

from ml.monitoring import HumanApproval, demote_on_drift, promote, psi
from ml.registry import ModelRecord, append, latest

NAMES = ("a", "b")


def _challenger(path: Path, verdict: str = "PASS") -> ModelRecord:
    m = ModelRecord(
        "exp", "f" * 64, "challenger", "d" * 64, "tob-v1", {"seed": 7}, {}, verdict=verdict
    )
    append(m, path)
    return m


def _approval(sha: str = "f" * 64) -> HumanApproval:
    return HumanApproval(
        "example-reviewer", sha, "https://github.com/o/r/pull/1", "2026-09-27T00:00Z"
    )


def test_psi_is_near_zero_for_same_distribution_and_large_for_a_shift() -> None:
    rng = np.random.default_rng(1)
    ref = rng.normal(size=5000)
    assert psi(ref, rng.normal(size=5000)) < 0.02
    assert psi(ref, rng.normal(loc=1.0, size=5000)) > 0.25


def test_drift_demotes_to_no_signal_and_records_it(tmp_path: Path) -> None:
    log = tmp_path / "models.jsonl"
    m = _challenger(log)
    rng = np.random.default_rng(2)
    ref = rng.normal(size=(4000, 2))
    assert demote_on_drift(m, ref, rng.normal(size=(4000, 2)), NAMES, path=log) is m
    shifted = rng.normal(size=(4000, 2)) + np.array([0.0, 2.0])
    out = demote_on_drift(m, ref, shifted, NAMES, path=log)
    assert out.status == "no_signal"
    assert "'b'" in out.reason
    rec = latest(m.artifact_sha256, log)
    assert rec is not None
    assert rec.status == "no_signal"


def test_promotion_refuses_without_human_approval(tmp_path: Path) -> None:
    log = tmp_path / "models.jsonl"
    m = _challenger(log)
    with pytest.raises(PermissionError, match="human-approval"):
        promote(m.artifact_sha256, None, path=log)
    with pytest.raises(PermissionError, match="different artifact"):
        promote(m.artifact_sha256, _approval("e" * 64), path=log)
    with pytest.raises(PermissionError, match="incomplete"):
        promote(m.artifact_sha256, HumanApproval(" ", m.artifact_sha256, "x", "t"), path=log)
    rec = latest(m.artifact_sha256, log)
    assert rec is not None
    assert rec.status == "challenger"
    promote(m.artifact_sha256, _approval(), path=log)
    rec = latest(m.artifact_sha256, log)
    assert rec is not None
    assert rec.status == "promoted"
    assert rec.approval["approver"] == "example-reviewer"


def test_demoted_model_cannot_be_promoted(tmp_path: Path) -> None:
    log = tmp_path / "models.jsonl"
    m = _challenger(log)
    rng = np.random.default_rng(3)
    demote_on_drift(m, rng.normal(size=(999, 2)), rng.normal(size=(999, 2)) + 3, NAMES, path=log)
    with pytest.raises(PermissionError, match="only a registered challenger"):
        promote(m.artifact_sha256, _approval(), path=log)


@pytest.mark.parametrize("verdict", ["FAIL", ""])  # "" is a legacy record written before verdicts
def test_promotion_refuses_a_challenger_without_a_walk_forward_pass(
    tmp_path: Path, verdict: str
) -> None:
    log = tmp_path / "models.jsonl"
    m = _challenger(log, verdict)
    with pytest.raises(PermissionError, match="walk-forward PASS"):
        promote(m.artifact_sha256, _approval(), path=log)
    rec = latest(m.artifact_sha256, log)
    assert rec is not None
    assert rec.status == "challenger"
