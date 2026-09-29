# data/schemas

Versioned schemas for recorded market data, defined as Arrow schemas in `__init__.py`: `L2_DELTA_V1` and `TRADE_V1`. They mirror `qc_core::BookDelta` and `qc_core::Trade`: prices and quantities are raw fixed-point `int64` at scale 1e8, and every row carries `exchange_ts` (venue time) and `local_ts` (our receipt time) in nanoseconds.

Each schema's metadata names it and its version. `check_schema` refuses a table whose columns or version differ. A breaking change adds `_V2` beside `_V1`; old versions are kept so recorded files stay readable.
