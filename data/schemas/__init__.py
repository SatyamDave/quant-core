"""Versioned Arrow schemas for recorded market data, mirroring `qc_core::BookDelta` and `Trade`.

Prices and quantities are raw fixed-point integers at `SCALE` (1e8), exactly as in
`engine/crates/core/src/fixed.rs`; floats never hold money. Timestamps are nanoseconds since
the Unix epoch: `exchange_ts` as stamped by the venue, `local_ts` when our recorder received it.
A breaking change adds a new version; old versions stay so recorded files remain readable.
"""

import pyarrow as pa

SCALE = 100_000_000
BUY, SELL = 0, 1


def _meta(name: str, version: int) -> dict[str, str]:
    return {"schema": name, "version": str(version), "scale": str(SCALE)}


L2_DELTA_V1 = pa.schema(
    [
        pa.field("instrument", pa.uint32(), nullable=False),
        pa.field("side", pa.uint8(), nullable=False),  # BUY or SELL
        pa.field("price", pa.int64(), nullable=False),
        pa.field("qty", pa.int64(), nullable=False),  # 0 removes the level
        pa.field("seq", pa.uint64(), nullable=False),  # venue sequence; a gap means resync
        pa.field("exchange_ts", pa.uint64(), nullable=False),
        pa.field("local_ts", pa.uint64(), nullable=False),
    ],
    metadata=_meta("l2_delta", 1),
)

TRADE_V1 = pa.schema(
    [
        pa.field("instrument", pa.uint32(), nullable=False),
        pa.field("aggressor", pa.uint8(), nullable=False),  # BUY or SELL
        pa.field("price", pa.int64(), nullable=False),
        pa.field("qty", pa.int64(), nullable=False),
        pa.field("exchange_ts", pa.uint64(), nullable=False),
        pa.field("local_ts", pa.uint64(), nullable=False),
    ],
    metadata=_meta("trade", 1),
)


def check_schema(table: pa.Table, expected: pa.Schema) -> None:
    """Refuse a table whose schema name, version, or columns differ from `expected`."""
    if not table.schema.equals(expected, check_metadata=True):
        raise ValueError(f"schema mismatch: got {table.schema}, expected {expected}")
