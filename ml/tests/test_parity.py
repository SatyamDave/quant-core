"""Python side of the Python/Rust parity check (Rust side: engine/crates/inference/tests)."""

import csv
import hashlib
import math
from pathlib import Path

from ml.features import TopOfBook, compute

FIXTURES = Path(__file__).parent / "fixtures"


def test_features_reproduce_committed_expected_values() -> None:
    tick = int((FIXTURES / "tick.txt").read_text())
    with (FIXTURES / "books.csv").open() as f:
        books = [TopOfBook(*map(int, row)) for row in list(csv.reader(f))[1:]]
    with (FIXTURES / "expected.csv").open() as f:
        expected = list(csv.reader(f))[1:]
    got = compute(books, tick)
    assert len(got) == len(expected)
    for row, want in zip(got, expected, strict=True):
        if not want[0]:
            assert row is None
            continue
        assert row is not None
        for g, w in zip(row, want[:4], strict=True):
            assert math.isclose(g, float(w), rel_tol=1e-9, abs_tol=0.0)


def test_model_artifact_matches_recorded_hash() -> None:
    digest = hashlib.sha256((FIXTURES / "model.json").read_bytes()).hexdigest()
    assert digest == (FIXTURES / "model.sha256").read_text().strip()
