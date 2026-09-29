# tests/replay

Recorded market days. Replaying a day must reproduce identical orders (root rule 7); a failure blocks merge.

- `sample_day.csv`: a synthetic day (not real market data) in the record format documented in `engine/crates/gateway/src/record.rs`, about 83 simulated minutes of one instrument with one deliberate sequence gap and resync. Regenerate with `python3 generate_sample_day.py` from this directory; the output is deterministic.
- `just replay` runs the sample engine (example quoter → risk → OMS → `SimVenue`) over the day twice and fails unless both order logs have the same sha256.
- The Rust test `engine/crates/replay/tests/replay.rs` runs the same comparison in `cargo test`.
