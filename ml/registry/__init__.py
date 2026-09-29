"""Model registry: one append-only JSONL record per model event, next to the trial log.

Uses the `research/registry` JSONL store. ADR-0005 (Proposed) names MLflow as the tracker and
registry; this JSONL store is what is implemented.
ponytail: JSONL on disk; move to an MLflow tracking server when several machines train at once
or the registry needs a UI. Records are never edited: a status change is a new record for the
same artifact hash, and the latest record wins.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from registry import REGISTRY_DIR, read, record

Status = Literal["challenger", "no_signal", "promoted"]

MODELS = REGISTRY_DIR / "models.jsonl"


@dataclass(frozen=True)
class ModelRecord:
    experiment: str
    artifact_sha256: str
    status: Status
    dataset_hash: str
    feature_version: str
    config: dict[str, object]  # the full training config, seed included
    metrics: dict[str, float]
    lineage: dict[str, str] = field(default_factory=dict)  # git commit, parent model, trial ids
    reason: str = ""  # why the status changed: drift report, approval reference
    approval: dict[str, str] = field(default_factory=dict)  # set only on promotion
    # Walk-forward verdict, PASS or FAIL. Records written before this field have "", which
    # promote() refuses like FAIL.
    verdict: str = ""


def latest(artifact_sha256: str, path: Path = MODELS) -> ModelRecord | None:
    rows = [r for r in read(path) if r["artifact_sha256"] == artifact_sha256]
    return ModelRecord(**rows[-1]) if rows else None


def append(entry: ModelRecord, path: Path = MODELS) -> None:
    record(path, entry)
