---
paths:
  - "engine/crates/orderbook/**"
  - "engine/crates/gateway/**"
  - "engine/crates/oms/**"
  - "engine/crates/strategy-runtime/**"
  - "engine/crates/inference/**"
---

# Rust hot path

- No heap allocation, locks, syscalls, or formatted logging (`format!`, `println!`, `tracing` with formatting) on the per-event path. Preallocate at startup.
- Money and quantities are `qc_core::Price` / `qc_core::Qty` (i64 fixed point, 1e8 scale). Never `f32`/`f64` for money.
- No `HashMap`/`HashSet` (banned in `engine/clippy.toml`): iteration order breaks replay determinism. Use `BTreeMap` or indexed `Vec`s.
- No network calls to AI APIs from any engine crate (rule 1).
- `unsafe` is denied workspace-wide. If a crate ever opts in, each block needs a `// SAFETY:` comment and a named reviewer.
- A hot-path change ships with a criterion bench in `engine/benches/`; a regression over 5% fails the PR. Run `just bench`.
- `just replay` must reproduce the recorded order log exactly (placeholder until the engine-core phase lands it).
- oms: match state enums exhaustively, no `_ =>` arms.
- inference: no model output is a guess; failure or timeout returns `None` ("no signal").
