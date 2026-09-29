"""Experiment registry: every trial is appended, including failures (root rule 6).

This stub writes JSON Lines to a local file so the trial count used for deflated
Sharpe is honest from the first experiment. ADR-0005 picks the real backend.

REGISTRY_DIR is the one ledger location every writer and reader uses: research/registry/log/,
or the directory in QC_REGISTRY_DIR when that is set.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

REGISTRY_DIR = Path(os.environ.get("QC_REGISTRY_DIR", Path(__file__).resolve().parent / "log"))
TRIALS = REGISTRY_DIR / "trials.jsonl"


@dataclass(frozen=True)
class Trial:
    experiment: str
    config_hash: str
    passed: bool
    metrics: dict[str, float]
    notes: str = ""


def record(path: Path, entry: "DataclassInstance") -> None:
    """Append one record (a Trial, or ml.registry.ModelRecord). The file is only ever appended."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), sort_keys=True) + "\n")


def read(path: Path) -> list[dict[str, Any]]:
    """All records in append order; an absent file is an empty registry."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def trial_count(path: Path, experiment: str) -> int:
    """Number of recorded trials for an experiment, failures included."""
    return sum(1 for r in read(path) if r["experiment"] == experiment)
