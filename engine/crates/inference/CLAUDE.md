# engine/crates/inference — qc-inference: deterministic model inference; no model means no signal

## Owns / does not own
- Owns: `Signal { edge_bps: i32 }`, the `Model` trait (`predict(&[f32]) -> Option<Signal>`), loading exported models and checking their registry hash, the Rust side of feature parity.
- Does not own: training, validation, or export (ml/), promotion decisions (humans, via promotion-dossier), feature definitions' source of truth (ml/features), the trade decision (`agent/`, ADR-0040) — this crate's output stays a signal, never an order.

## Commands
- `cargo test -p qc-inference --locked`
- `cargo clippy -p qc-inference --all-targets --locked -- -D warnings`
- `cargo bench --workspace --locked -- --baseline main` for any `predict` change
- Feature parity against Python: see ml/CLAUDE.md (parity failure blocks merge)

## MUST
- Load only models exported by ml/export whose artifact hash matches the registry entry; a mismatch refuses to load.
- `predict` is deterministic: same features, same model, same output (tested).
- `predict` stays within its latency budget, shown by a criterion bench.
- Missing or non-finite features, a wrong feature count, a stale model, or any doubt returns `None`. Check: the `bad_input_means_no_signal` style test exists for every model.
- Money and edge leave this crate as integers (`edge_bps: i32`); `f32` stays inside feature math.

## NEVER
- Never guess a signal as fallback; the fallback is `None`.
- No network or AI API calls, no model downloads at runtime (root rule 1). Check: `grep -rniE 'http|reqwest|anthropic|openai' src Cargo.toml` prints nothing.
- No `f64` in `Signal` or anything returned to strategies.
- Never load a model file from outside the exported, hashed artifact path.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- ml/CLAUDE.md, ml/export/README.md, skill: promotion-dossier (ml/.claude/skills/)
- ADR-0040 (agent handoff): docs/adr/0040-agentic-decision.md
