//! Shared test fixtures. Not a test binary itself (see `tests/*.rs`'s `mod support;`).
//! Included into every test binary separately, so any one binary using only
//! some of these functions is not dead code.
#![allow(dead_code)]

use qc_bridge::instrument::Instrument;
use qc_bridge::{BridgeEngine, VenueMode};
use qc_core::Qty;
use qc_risk::{KillSwitch, Limits};

/// The workspace's default limits, same file `qc_risk`'s own tests load. Deliberately tiny
/// (root CLAUDE.md rule 4) — not sized for the committed sample day's exercised volume.
#[must_use]
pub fn limits() -> Limits {
    toml::from_str(include_str!("../../../../../config/limits/default.toml")).unwrap()
}

/// Fixture-only limits for tests that replay the committed sample day
/// (`sample_day_csv`, ~$92k of accepted-order notional in one simulated day, by design). Not
/// the production default: see `tests/replay/limits.toml`.
#[must_use]
pub fn sample_day_limits() -> Limits {
    toml::from_str(include_str!("../../../../../tests/replay/limits.toml")).unwrap()
}

/// A tiny synced book (bid 100.0 / ask 100.2, tick 0.1) plus one more delta so
/// a `decide_every: 1` engine is ready after two records. Kept separate from
/// the committed sample day so risk and protocol tests can reason about exact
/// prices without depending on the fixture's contents.
#[must_use]
pub fn small_book_csv() -> String {
    "S,1,10,1000,1000,100.0@1;99.9@1,100.2@1;100.3@1\n\
     D,1,11,B,99.8,1,2000,2000\n"
        .to_owned()
}

/// The committed sample day used by `just replay`, for determinism and
/// schema-breadth tests.
#[must_use]
pub fn sample_day_csv() -> String {
    std::fs::read_to_string(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../tests/replay/sample_day.csv"
    ))
    .expect("tests/replay/sample_day.csv is committed at the repo root")
}

#[must_use]
pub fn engine_from(csv: &str, decide_every: u32) -> BridgeEngine {
    engine_from_with_limits(csv, decide_every, limits())
}

#[must_use]
pub fn engine_from_with_limits(csv: &str, decide_every: u32, limits: Limits) -> BridgeEngine {
    let records = BridgeEngine::load_recording(csv).expect("fixture csv parses");
    BridgeEngine::new(
        records,
        limits,
        KillSwitch::new(),
        "0.1".parse().unwrap(),
        decide_every,
        None,
        VenueMode::Sim,
    )
}

/// The committed sample instrument (issue #66): parses the real
/// `config/instruments/spy.toml` and `config/limits/spy.toml`, so this test
/// helper also proves those committed files parse.
#[must_use]
pub fn spy_instrument() -> Instrument {
    let dir = concat!(env!("CARGO_MANIFEST_DIR"), "/../../../config/instruments");
    let text = std::fs::read_to_string(format!("{dir}/spy.toml")).expect("spy.toml is committed");
    Instrument::parse(&text, std::path::Path::new(dir)).expect("spy.toml parses")
}

#[must_use]
pub fn spy_limits() -> Limits {
    toml::from_str(include_str!("../../../../../config/limits/spy.toml")).unwrap()
}

/// An engine configured with `--instrument` (protocol v1.2, issue #66).
#[must_use]
pub fn engine_with_instrument(csv: &str, decide_every: u32) -> BridgeEngine {
    let records = BridgeEngine::load_recording(csv).expect("fixture csv parses");
    BridgeEngine::with_instrument(
        records,
        spy_limits(),
        KillSwitch::new(),
        "0.01".parse().unwrap(),
        decide_every,
        None,
        VenueMode::Sim,
        None,
        Some(spy_instrument()),
    )
}

#[must_use]
pub fn engine_with_kill_switch(
    csv: &str,
    decide_every: u32,
    kill_switch: KillSwitch,
) -> BridgeEngine {
    let records = BridgeEngine::load_recording(csv).expect("fixture csv parses");
    BridgeEngine::new(
        records,
        limits(),
        kill_switch,
        "0.1".parse().unwrap(),
        decide_every,
        None,
        VenueMode::Sim,
    )
}

/// Same as [`engine_from`] but started with `--venue external` (protocol
/// v1.1, issues #34/#29): an accepted intent is recorded `PendingNew` and
/// never routed to `SimVenue`, and the result carries a signed `approval`.
#[must_use]
pub fn engine_external(csv: &str, decide_every: u32) -> BridgeEngine {
    let records = BridgeEngine::load_recording(csv).expect("fixture csv parses");
    BridgeEngine::new(
        records,
        limits(),
        KillSwitch::new(),
        "0.1".parse().unwrap(),
        decide_every,
        None,
        VenueMode::External,
    )
}

/// Same as [`engine_external`] but with the protocol v1.2 `reconcile` gate
/// (issue #45) already satisfied with an empty, matching venue snapshot, so
/// a test about approvals/`report_execution` is not also a test of the
/// unreconciled gate (that gate has its own test: `tests/reconcile.rs`).
#[must_use]
pub fn engine_external_reconciled(csv: &str, decide_every: u32) -> BridgeEngine {
    let mut engine = engine_external(csv, decide_every);
    let result = engine.reconcile_with_venue(&[], Qty::ZERO);
    assert_eq!(
        result["ok"], true,
        "empty venue snapshot must reconcile cleanly"
    );
    engine
}
