# engine/crates/core — qc-core: fixed-point prices and quantities, IDs, clocks, events

## Owns / does not own
- Owns: `Price` / `Qty` (i64, SCALE 1e8) in fixed.rs, typed IDs in id.rs, `Timestamp` (u64 ns), `Clock` / `SimClock` in clock.rs, `BookDelta`, `Trade`, `MarketEvent`, `OrderEvent`, `OrderType`, `OrderRequest` in event.rs.
- Does not own: book state (qc-orderbook), order lifecycle (qc-oms), limits (qc-risk). No logic beyond the types' own arithmetic and conversions.

## Commands
- `cargo test -p qc-core --locked`
- `cargo clippy -p qc-core --all-targets --locked -- -D warnings`
- `cargo test --workspace --locked` after any public type change (every crate depends on this one)

## MUST
- Fixed-point arithmetic is checked: overflow returns `None` or an error, never wraps. Check: `grep -nE 'wrapping_|as i64|as u64' src/fixed.rs` hits only reviewed, tested conversions.
- Every `BookDelta` carries `seq`, the exchange timestamp, and the local timestamp.
- Engine time comes from a `Clock`; replay uses `SimClock`. Check: `grep -rn --include='*.rs' 'SystemTime::now\|Instant::now' ..` hits only the real `Clock` impl.
- Every new public type has a unit test in the same file.

## NEVER
- No `f32` / `f64` for money or quantities. Check: `grep -rnwE 'f32|f64' src | grep -v '//'` prints nothing.
- No dependencies beyond `serde` without an ADR. Check: the `[dependencies]` table in Cargo.toml.
- No I/O, allocation-heavy types, or `HashMap` in public types (they sit on every hot path).

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- ../../CLAUDE.md (engine rules), .claude/rules/rust-hotpath.md, docs/ARCHITECTURE.md
