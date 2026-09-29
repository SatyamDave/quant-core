# ml/export

`export` writes a multinomial linear model as a JSON artifact (`qc-linear-v1`) and returns its bytes and sha256. `engine/crates/inference` loads it only when the sha256 equals the registry hash and the feature version and names match its own, and returns no signal on NaN, infinite, or missing features.
