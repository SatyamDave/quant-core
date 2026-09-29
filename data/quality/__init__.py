"""Data-quality checks run on ingest: sequence gaps, duplicates, clock skew.

Each check returns row indices so a report can point at the exact records. A dataset
builder refuses input with gaps or duplicates, because the reconstructed book would be wrong.
"""

from dataclasses import dataclass

import numpy as np
import pyarrow as pa


@dataclass(frozen=True)
class QualityReport:
    seq_gaps: list[int]  # row i where seq[i] != seq[i-1] + 1 (and not a duplicate)
    duplicate_seqs: list[int]  # row i where seq[i] <= seq[i-1]
    time_gaps: list[int]  # row i where exchange_ts jumped more than max_gap_ns
    clock_skew: list[int]  # row i where local_ts < exchange_ts or latency > max_latency_ns

    @property
    def ok(self) -> bool:
        return not (self.seq_gaps or self.duplicate_seqs or self.clock_skew)


def _col(table: pa.Table, name: str) -> np.ndarray:
    return np.asarray(table.column(name).to_numpy(), dtype=np.int64)


def check_deltas(
    table: pa.Table, max_gap_ns: int = 60_000_000_000, max_latency_ns: int = 1_000_000_000
) -> QualityReport:
    """Checks an L2 delta table in recorded (local receipt) order."""
    seq = _col(table, "seq")
    exch = _col(table, "exchange_ts")
    local = _col(table, "local_ts")
    step = np.diff(seq)
    rows = np.arange(1, len(seq))
    latency = local - exch
    return QualityReport(
        seq_gaps=rows[step > 1].tolist(),
        duplicate_seqs=rows[step <= 0].tolist(),
        time_gaps=rows[np.diff(exch) > max_gap_ns].tolist(),
        clock_skew=np.flatnonzero((latency < 0) | (latency > max_latency_ns)).tolist(),
    )
