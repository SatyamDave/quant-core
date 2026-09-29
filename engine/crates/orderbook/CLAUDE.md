# engine/crates/orderbook — qc-orderbook: per-instrument L2 book built from BookDeltas

## Owns / does not own
- Owns: `OrderBook`, `BookError` (`WrongInstrument`, `SequenceGap`), applying deltas, best bid/ask.
- Does not own: parsing venue messages (qc-gateway), resync requests to the venue (qc-gateway on `SequenceGap`), signals (qc-strategy-runtime).

## Commands
- `cargo test -p qc-orderbook --locked`
- `cargo clippy -p qc-orderbook --all-targets --locked -- -D warnings`
- `cargo bench -p qc-benches --locked --bench orderbook -- --baseline main`
- Fuzz (nightly only, from engine/fuzz): `cargo +nightly fuzz run <target>`

## MUST
- After every successful `apply` the book is not crossed: best bid < best ask when both exist. A test asserts it after every update.
- A skipped or repeated `seq` returns `BookError::SequenceGap` and leaves the book unchanged; the caller resyncs from a snapshot.
- On any error the book is unchanged (tested per error variant).
- A parser or `apply` change adds property tests and a fuzz target in engine/fuzz. Check: the PR diff touches `engine/fuzz/`.
- Any change to `apply` pastes `--baseline main` bench output in the PR; over 5% slower fails.

## NEVER
- No `HashMap` / `HashSet` for levels (clippy.toml bans them).
- No `f64` prices. Check: `grep -rnwE 'f32|f64' src | grep -v '//'` prints nothing.
- Never paper over a gap by applying the delta anyway. Check: `grep -n 'SequenceGap' src/lib.rs` shows it returned before any mutation.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- ../../CLAUDE.md, .claude/rules/rust-hotpath.md, engine/benches/benches/orderbook.rs, engine/fuzz/README.md
