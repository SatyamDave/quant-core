# engine/fuzz

cargo-fuzz targets for venue message parsers and the order book. cargo-fuzz needs a nightly toolchain, so this directory is deliberately not a member of the engine workspace (the workspace uses the floating stable channel). Create targets with `cargo +nightly fuzz init` from here when the first parser lands; CI runs a nightly smoke pass (Phase 4).
