# data/quality

Data-quality checks run on ingest: sequence gaps, duplicate or backwards sequence numbers, exchange-time gaps, and clock skew (local receipt before exchange time, or latency above a bound). `check_deltas` returns the offending row indices; `ml.datasets` refuses a file with gaps, duplicates, or skew. Tests are in `tests/`.
