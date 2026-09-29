import pyarrow as pa

from data.quality import check_deltas
from data.schemas import L2_DELTA_V1


def _table(seq: list[int], exch: list[int], local: list[int]) -> pa.Table:
    n = len(seq)
    return pa.Table.from_pydict(
        {
            "instrument": [1] * n,
            "side": [0] * n,
            "price": [100] * n,
            "qty": [1] * n,
            "seq": seq,
            "exchange_ts": exch,
            "local_ts": local,
        },
        schema=L2_DELTA_V1,
    )


def test_clean_data_passes() -> None:
    report = check_deltas(_table([1, 2, 3], [10, 20, 30], [15, 25, 35]))
    assert report.ok
    assert report.time_gaps == []


def test_gap_duplicate_and_skew_are_located() -> None:
    report = check_deltas(
        _table([1, 2, 2, 5, 6], [10, 20, 30, 40, 200], [15, 25, 29, 45, 205]),
        max_gap_ns=100,
    )
    assert report.duplicate_seqs == [2]
    assert report.seq_gaps == [3]
    assert report.clock_skew == [2]  # local_ts before exchange_ts
    assert report.time_gaps == [4]
    assert not report.ok


def test_excess_latency_is_skew() -> None:
    report = check_deltas(_table([1, 2], [10, 20], [15, 20 + 2_000]), max_latency_ns=1_000)
    assert report.clock_skew == [1]
