# ADR-0005: Data storage

## Status

Proposed. Date proposed: 2026-09-27. Not yet approved by a human.

## Context

The platform will record tick and level-2 order book data from venues, replay it deterministically through the engine (root rule 7), build point-in-time research datasets from it, and record every experiment and model (root rule 6). Raw market data is large and must never be committed to git (`.gitignore` already excludes `data/raw/`). The bootstrap spec proposes Parquet in object storage for tick and L2 data, DuckDB or Polars for research queries, a catalog in `data/catalog/`, and MLflow or an equivalent for experiments and the model registry.

Facts checked on 2026-09-27:

- NautilusTrader's data catalog "stores NautilusTrader data in Parquet files for backtesting, live trading, and research", uses Arrow schemas defined in its Rust crates, has writers for order book deltas and depth snapshots, and accepts local paths and S3, GCS and Azure object-store URIs ([data concepts](https://nautilustrader.io/docs/latest/concepts/data/)).
- DuckDB reads Parquet from S3 through its httpfs extension and uses Parquet metadata plus HTTP range requests to download only the parts a query needs ([S3 API support](https://duckdb.org/docs/lts/core_extensions/httpfs/s3api), [S3 Parquet import](https://duckdb.org/docs/lts/guides/network_cloud_storage/s3_import)).
- The MLflow Model Registry provides model versioning, aliases (for example `models:/MyModel@champion`), tags, annotations and lineage back to the run that produced each version ([Model Registry docs](https://mlflow.org/docs/latest/ml/model-registry/)).
- Licenses: pyarrow Apache-2.0, polars MIT, duckdb MIT, mlflow Apache-2.0 (see ADR-0002).

## Decision

1. **Raw and normalized market data is stored as Parquet in object storage**, partitioned by venue, instrument, data type and date. Each file records both the exchange timestamp and the local receive timestamp (spec §5, data/). Where NautilusTrader's catalog layout and Arrow schemas cover a data type, we use them rather than inventing a parallel format, so the recorder, backtester and replay test read the same files.
2. **Object storage is the source of truth; local copies are caches.** Buckets are versioned or write-once for raw data, so a recorded day cannot be edited after the fact. Which cloud provider hosts the bucket is left to the Phase 1 or ops work that provisions it, and depends on the venue region chosen in ADR-0003.
3. **`data/catalog/` in git holds metadata only**: dataset manifests with content hashes, schema versions, time ranges and data-quality results. It never holds data files.
4. **Research queries use DuckDB and Polars over the Parquet files.** Both are allowed; DuckDB suits ad hoc SQL over object storage, and Polars suits dataframe pipelines in `ml/`. Datasets used for training are materialized with a content hash recorded in the catalog, so a model can name exactly what it was trained on.
5. **MLflow is the experiment tracker and model registry**, self-hosted, with its backing store and artifact store under our control. Every backtest, walk-forward and training run logs to it, including failures. Registry entries carry the dataset hash, feature version and artifact hash that `engine/crates/inference` checks before loading a model (spec §5 and §9). Aliases (for example `champion`, `challenger`, `shadow`) mark lifecycle position; changing the alias that points to live capital requires human approval, which Phase 6 enforces.
6. **Track-record records** are append-only and separate from research storage; their design is out of scope here.

## Alternatives

- **A time-series database (for example kdb+, QuestDB, TimescaleDB, ClickHouse) as the primary tick store.** Faster interactive queries, but each adds a stateful service to operate, and their licenses would need review under ADR-0002. Parquet in object storage is cheaper, portable and already what NautilusTrader reads. A query service can be added later on top of the same files.
- **Store data in git LFS or in the repo.** Rejected: the spec forbids committing raw data, and git is not built for this volume.
- **Weights & Biases or another hosted tracker instead of MLflow.** Good tooling, but it sends experiment metadata to a third party and adds a vendor dependency for the registry that gates model promotion. A self-hosted, Apache-2.0 registry keeps that control in-house.
- **A hand-rolled experiment registry (for example rows in DuckDB).** Possible for the first weeks, but MLflow already provides versioning, aliases and lineage that we would otherwise rebuild.

## Consequences

- We must run and back up an MLflow server and its database. Losing it would lose the experiment history that rule 6 depends on, so its backup is part of the ops work.
- Object storage costs grow with recording volume; retention and tiering policy is a later decision.
- Tying our schemas to NautilusTrader's catalog means a NautilusTrader upgrade can change stored schemas. The pin move PR (ADR-0001) must run the replay test against previously recorded data.
- The object-storage region should match the trading region chosen in ADR-0003 to keep recorders simple; that is open until ADR-0003 is approved.

## Review date

When the first recorder writes real data, or 2026-12-27, whichever comes first.
