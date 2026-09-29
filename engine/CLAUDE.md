# engine/ — deterministic Rust hot path: book, gateway, OMS, risk, strategy runtime, inference

## Owns / does not own
- Owns: the Cargo workspace (`crates/*` named `qc-*`, `benches/`), `fuzz/` (not a workspace member; needs nightly), clippy.toml, rust-toolchain.toml.
- Does not own: risk limit values (`config/limits/`, protected), model training (`ml/`), deploys (`ops/deploy/`), the final trade decision (`agent/`, ADR-0040) — the engine only emits a `DecisionRequest` and accepts submitted intents back through `qc-bridge`.

## Commands
- `cargo fmt --all --check`
- `cargo clippy --workspace --all-targets --locked -- -D warnings`
- `cargo test --workspace --locked` (one crate: `cargo test -p qc-oms --locked`)
- `cargo bench --workspace --locked -- --save-baseline main` on main, then `-- --baseline main` on your branch
- `just replay` and `just check` (just finds the root justfile from here)

## MUST
- Money is `qc_core::Price` / `Qty` (i64 fixed point, SCALE 1e8). Check: `grep -rnwE 'f32|f64' crates/{core,orderbook,gateway,oms,risk,strategy-runtime}/src | grep -v '//'` prints nothing (doc comments may mention floats).
- Every hot-path change (orderbook, gateway, oms, risk, strategy-runtime, inference) adds or updates a criterion bench in `benches/benches/` and pastes the `--baseline main` output in the PR; a regression over 5% fails CI.
- Every `unsafe` block has a `// SAFETY:` comment on the line above and a named reviewer sign-off in the PR. Check: `grep -rn -B1 'unsafe' crates/` shows a SAFETY line for each hit.
- `just replay` passes on every PR (root rule 7).
- New crates inherit workspace lints: `[lints] workspace = true`. Check: `grep -L 'workspace = true' crates/*/Cargo.toml` prints nothing.
- The agent service's only path in or out is `qc-bridge` (ADR-0040): it can submit an order intent, never place one directly, and `qc-risk` plus the kill switch run before `qc-oms` sees it — risk stays authoritative.

## NEVER
- No allocation, locks, syscalls, or formatted logging on the hot path. Check: `grep -rnE 'Mutex|RwLock|println!|eprintln!|format!|std::fs|std::thread::sleep' crates/{orderbook,gateway,oms,risk,strategy-runtime,inference}/src` has no hit outside `#[cfg(test)]` modules.
- No `HashMap` / `HashSet` (random iteration breaks replay); clippy.toml bans them, so `cargo clippy ... -D warnings` fails.
- No AI or LLM client in any crate (root rule 1). Check: `grep -rniE 'anthropic|openai|llm' Cargo.lock crates/*/Cargo.toml` prints nothing.
- No crate-level `#![allow(unsafe_code)]` without a PR that names the reviewer. Check: `grep -rn 'allow(unsafe_code)' crates/`.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- Skills: engine/.claude/skills/ (add-venue-adapter, add-order-type, latency-profile, replay-debug)
- Rule: .claude/rules/rust-hotpath.md; ADR-0001 (engine foundation), ADR-0004 (toolchain) in docs/adr/
- docs/ARCHITECTURE.md; ADR-0040 (agent handoff): docs/adr/0040-agentic-decision.md
