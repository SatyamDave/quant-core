"""The snapshot check that decides whether a data/raw/study-0001 is run 0001's snapshot."""

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
        (raw / "listing" / "day_2026-09-10.json").write_text("{}")
    else:
        (raw / "requests.jsonl").write_text("")
    assert len(study.snapshot_problems(raw, catalog)) == 1


RUN_0001_LEDGER = "1898cf1317d4234204f7912f3d90ef63d06e7de7189ee29c00a5b3f481d06b08"


def test_committed_catalog_is_still_run_0001() -> None:
    """fetch.py must never overwrite the committed catalog with a new snapshot's hashes."""
    cat = json.loads(study.CATALOG.read_text())
    assert (cat["dataset"], cat["ledger_sha256"]) == ("study-0001", RUN_0001_LEDGER)
