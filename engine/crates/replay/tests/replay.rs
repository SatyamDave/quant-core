//! Rule 7: replaying the committed sample day gives the committed order log,
//! checked by its sha256, the same file `just replay` compares against.

use std::fmt::Write as _;

use qc_gateway::record;
use qc_risk::Limits;
use sha2::{Digest, Sha256};

const DAY: &str = include_str!("../../../../tests/replay/sample_day.csv");
// Fixture-only limits (headroom for this day's ~$92k exercised notional), not the tiny
// production default in config/limits/default.toml. See tests/replay/limits.toml.
const LIMITS: &str = include_str!("../../../../tests/replay/limits.toml");
const EXPECTED_SHA256: &str = include_str!("../../../../tests/replay/expected_order_log.sha256");

#[test]
fn sample_day_replays_identically_and_exercises_the_engine() {
    let records = record::parse(DAY).unwrap();
    let limits: Limits = toml::from_str(LIMITS).unwrap();
    let first = qc_replay::replay(&records, limits.clone());
    let second = qc_replay::replay(&records, limits);
    assert_eq!(first, second, "replay is not deterministic");
    let hash = Sha256::digest(first.as_bytes())
        .iter()
        .fold(String::new(), |mut hex, b| {
            let _ = write!(hex, "{b:02x}");
            hex
        });
    assert_eq!(
        hash,
        EXPECTED_SHA256.trim(),
        "the order log changed; if intended, update tests/replay/expected_order_log.sha256 and say why in the commit"
    );

    let count = |word: &str| {
        first
            .lines()
            .filter(|l| l.split(' ').nth(1) == Some(word))
            .count()
    };
    assert!(count("submit") > 100, "the strategy should trade");
    assert!(first.contains("Filled"), "some quotes should fill");
    assert!(
        first.contains("SequenceGap"),
        "the recorded gap should be detected"
    );
    assert_eq!(
        count("halt"),
        0,
        "the sample day must not halt:\n{}",
        first
            .lines()
            .filter(|l| l.contains("halt") || l.contains("reconcile"))
            .collect::<Vec<_>>()
            .join("\n")
    );
    assert_eq!(count("oms"), 0, "no illegal order events");
}
