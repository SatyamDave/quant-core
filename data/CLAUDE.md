# data/ — schemas, recorders, catalog, and data-quality checks (never the data itself)

## Owns / does not own
- Owns: schemas/ (versioned schemas for market data and engine logs — `data/schemas/`, distinct from the top-level `schemas/` decision handoff contract, ADR-0040), recorders/ (write Parquet with exchange and local timestamps), catalog/ (what exists, where it lives in object storage, schema version), quality/ (checks on ingest).
- Does not own: raw data (object storage, per ADR-0005), datasets for training (ml/datasets), venue connections for trading (engine/crates/gateway).

## Commands
- Committed data files: `git ls-files | grep -E '\.(parquet|csv|arrow|feather|zst|gz)$'` prints nothing
- Tests: `just test`

## MUST
- Every schema carries a version; a breaking change adds a new version, it never edits the old one in place.
- Recorders write Parquet with both the exchange timestamp and the local receive timestamp, plus the venue sequence number.
- Ingest runs quality checks for gaps, duplicates, and clock skew, and records the result in the catalog.
- Every catalog entry names its schema version and its object-storage location.
- Recorders read market data only; they need no trading permissions.

## NEVER
- Never commit data files (check above; `data/raw/` and `*.parquet` are gitignored).
- Never edit recorded data after the fact; corrections are new versions.
- No credentials in recorder configs (root rule 2).

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- ADR-0005 (data storage); ml/datasets/README.md; docs/ARCHITECTURE.md
- ADR-0040 (agent handoff, top-level schemas/): docs/adr/0040-agentic-decision.md
