"""Drift monitoring. Demotion to "no signal" is automatic; promotion never is.

Demoting only ever removes authority, so it needs no approval and writes its record at once.
Promotion refuses to run unless the record's walk-forward verdict is PASS and a human-approval
record for exactly this artifact is supplied. The code checks only that the approval is present
and consistent. `HumanApproval` is a plain record any caller, agent included, can construct;
nothing in code or repository settings enforces that a human wrote it (CODEOWNERS is not
enforced on this plan). Only the owner's merge discipline stands behind it today.
"""

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ml.registry import MODELS, ModelRecord, append, latest

PSI_DEMOTE = 0.25  # conventional "significant shift" level for PSI


def psi(reference: np.ndarray, current: np.ndarray, bins: int = 10) -> float:
    """Population stability index of `current` against decile bins of `reference`."""
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    edges[0], edges[-1] = -np.inf, np.inf
    eps = 1e-6
    ref = np.histogram(reference, edges)[0] / len(reference) + eps
    cur = np.histogram(current, edges)[0] / len(current) + eps
    return float(np.sum((cur - ref) * np.log(cur / ref)))


def demote_on_drift(
    model: ModelRecord,
    reference: np.ndarray,
    current: np.ndarray,
    names: tuple[str, ...],
    threshold: float = PSI_DEMOTE,
    path: Path = MODELS,
) -> ModelRecord:
    """Records `no_signal` if any feature's PSI exceeds `threshold`; otherwise returns `model`."""
    scores = {n: psi(reference[:, i], current[:, i]) for i, n in enumerate(names)}
    drifted = {n: s for n, s in scores.items() if s > threshold}
    if not drifted or model.status == "no_signal":
        return model
    demoted = dataclasses.replace(
        model, status="no_signal", reason=f"psi above {threshold}: {drifted}", approval={}
    )
    append(demoted, path)
    return demoted


@dataclass(frozen=True)
class HumanApproval:
    approver: str  # GitHub handle of the human who approved
    artifact_sha256: str  # the exact artifact approved
    dossier: str  # link to the promotion dossier PR
    approved_at: str  # ISO 8601


def promote(artifact_sha256: str, approval: HumanApproval | None, path: Path = MODELS) -> None:
    """Promotes a registered challenger that passed walk-forward, given a matching approval."""
    if approval is None:
        raise PermissionError("promotion requires a human-approval record")
    if not (approval.approver.strip() and approval.dossier.strip() and approval.approved_at):
        raise PermissionError("approval record is incomplete")
    if approval.artifact_sha256 != artifact_sha256:
        raise PermissionError("approval is for a different artifact")
    current = latest(artifact_sha256, path)
    if current is None or current.status != "challenger":
        raise PermissionError(f"only a registered challenger can be promoted, got {current}")
    if current.verdict != "PASS":
        raise PermissionError(f"promotion requires a walk-forward PASS, got {current.verdict!r}")
    append(
        dataclasses.replace(
            current,
            status="promoted",
            reason=f"approved by {approval.approver}",
            approval=dataclasses.asdict(approval),
        ),
        path,
    )
