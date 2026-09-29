# Copied with attribution from research/studies/0001-favorite-longshot/tests/test_snapshot.py
# study 0001's own test file is not modified.
"""The snapshot check that decides whether a data/raw/study-0002 is this run's snapshot."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import study


def make(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "raw"
    (raw / "listing").mkdir(parents=True)
    (raw / "listing" / "manifest.json").write_text("{}")
    (raw / "requests.jsonl").write_text('{"status": 200}\n')
    catalog = tmp_path / "catalog.json"

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    catalog.write_text(
        json.dumps(
            {
                "files": {"listing/manifest.json": sha(raw / "listing" / "manifest.json")},
                "ledger_sha256": sha(raw / "requests.jsonl"),
            }
        )
    )
    return raw, catalog


def test_matching_snapshot_has_no_problems(tmp_path: Path) -> None:
    raw, catalog = make(tmp_path)
    assert study.snapshot_problems(raw, catalog) == []


def test_absent_snapshot_is_reported(tmp_path: Path) -> None:
    _, catalog = make(tmp_path)
    missing = tmp_path / "nope"
    assert study.snapshot_problems(missing, catalog) == [f"{missing} does not exist"]


@pytest.mark.parametrize("change", ["edit", "delete", "extra", "ledger"])
def test_any_difference_is_a_different_snapshot(tmp_path: Path, change: str) -> None:
    raw, catalog = make(tmp_path)
    if change == "edit":
        (raw / "listing" / "manifest.json").write_text('{"x": 1}')
    elif change == "delete":
        (raw / "listing" / "manifest.json").unlink()
    elif change == "extra":
        (raw / "listing" / "series_KXEXTRA.json").write_text("{}")
    else:
        (raw / "requests.jsonl").write_text("")
    assert len(study.snapshot_problems(raw, catalog)) == 1


RUN_0002_LEDGER = "82748e3dc4f7b9dbcfb925b708c8361519d869b74f7f3703e792a8f4a2a9f1fb"


def test_committed_catalog_is_still_run_0002() -> None:
    """fetch.py must never overwrite the committed catalog with a new snapshot's hashes."""
    cat = json.loads(study.CATALOG.read_text())
    assert (cat["dataset"], cat["ledger_sha256"]) == ("study-0002", RUN_0002_LEDGER)
