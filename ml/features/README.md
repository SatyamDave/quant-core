# ml/features

Feature set `tob-v1`, the single definition used by Python training and Rust inference (`engine/crates/inference/src/features.rs`): order-flow imbalance over 10 events normalized by depth, queue imbalance, microprice deviation in ticks, and spread in ticks.

Parity: `ml/tests/regen_fixtures.py` writes `ml/tests/fixtures/` (books, expected features and probabilities, a model artifact and its hash). `ml/tests/test_parity.py` and `engine/crates/inference/tests/parity.rs` both check against it within 1e-9 relative, in `just test`. Changing a formula means a new `FEATURE_VERSION` in both languages and a regenerated fixture.
