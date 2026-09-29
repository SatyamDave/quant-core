"""Regenerates the Python/Rust parity fixture in ml/tests/fixtures/. Deterministic.

Run from research/: `PYTHONPATH=..:. uv run python ../ml/tests/regen_fixtures.py`.
Only regenerate on a deliberate feature or export change (new FEATURE_VERSION); the parity
tests exist to catch every other change.
"""

import csv
import tempfile
from pathlib import Path

import numpy as np

from data.recorders.synthetic import TICK, write_day
from ml.datasets import build, replay_top_of_book
from ml.export import export
from ml.features import compute
from ml.training import TrainConfig, train

FIXTURES = Path(__file__).parent / "fixtures"
N_EVENTS = 800
HORIZON = 20


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        day = write_day(Path(tmp), seed=11, n_events=N_EVENTS, date="2026-01-05")
        books = replay_top_of_book(day / "l2_delta.parquet")[:N_EVENTS]
    ds = build(books, TICK, HORIZON, source="synthetic-seed-11")
    model = train(ds.X, ds.y, TrainConfig(C=1.0))
    artifact, sha = export(model)
    FIXTURES.mkdir(exist_ok=True)
    (FIXTURES / "model.json").write_bytes(artifact)
    (FIXTURES / "model.sha256").write_text(sha + "\n")
    (FIXTURES / "tick.txt").write_text(f"{TICK}\n")
    with (FIXTURES / "books.csv").open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["bid_px", "bid_qty", "ask_px", "ask_qty"])
        w.writerows([b.bid_px, b.bid_qty, b.ask_px, b.ask_qty] for b in books)
    # Expected output per book row: four features then down/flat/up probabilities, or all
    # empty where the feature set is missing (warm-up or invalid book).
    feats = compute(books, TICK)
    with (FIXTURES / "expected.csv").open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["ofi_norm", "queue_imbalance", "microprice_dev_ticks", "spread_ticks",
                    "p_down", "p_flat", "p_up"])  # fmt: skip
        for row in feats:
            if row is None:
                w.writerow([""] * 7)
                continue
            probs = model.predict_proba(np.array([row]))[0]
            w.writerow([repr(v) for v in (*row, *(float(p) for p in probs))])


if __name__ == "__main__":
    main()
