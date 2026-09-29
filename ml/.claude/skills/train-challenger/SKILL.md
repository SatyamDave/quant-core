---
name: train-challenger
description: Train a challenger model against the champion. Use when retraining, training a new model, or comparing a candidate to the current champion in ml/.
---
## Steps
1. Build the dataset with the point-in-time builder in ml/datasets/ and record its hash.
2. Train with fixed seeds; log the full config, dataset hash, and feature version to the registry.
3. Validate with purged, embargoed walk-forward on the same data as the champion (see research skill run-walkforward).
4. Run the Python vs Rust feature parity test; failure blocks.
5. Record the challenger in the registry with metrics and artifact hash. Never promote it; that needs promotion-dossier and a human.
