//! Python/Rust parity on the committed fixture from `ml/tests/regen_fixtures.py`.
//! A failure here means the two feature or model implementations disagree.

use std::fs;
use std::path::PathBuf;

use qc_core::{Price, Qty};
use qc_inference::Model;
use qc_inference::features::{FeatureState, N_FEATURES, TopOfBook};
use qc_inference::linear::{LinearModel, LoadError};

const REL_TOL: f64 = 1e-9;

fn fixture(name: &str) -> String {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../ml/tests/fixtures");
    fs::read_to_string(dir.join(name)).unwrap_or_else(|e| panic!("read fixture {name}: {e}"))
}

fn model_bytes() -> Vec<u8> {
    fixture("model.json").into_bytes()
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() <= REL_TOL * a.abs().max(b.abs())
}

fn rows(name: &str) -> Vec<Vec<String>> {
    fixture(name)
        .lines()
        .skip(1)
        .map(|l| l.split(',').map(str::to_owned).collect())
        .collect()
}

#[test]
fn features_and_probabilities_match_python() {
    let tick = Price::from_raw(fixture("tick.txt").trim().parse().expect("tick"));
    let model = LinearModel::load(&model_bytes(), &fixture("model.sha256")).expect("load");
    let mut state = FeatureState::new(tick);
    let books = rows("books.csv");
    let expected = rows("expected.csv");
    assert_eq!(books.len(), expected.len());
    let mut compared = 0;
    for (i, (b, want)) in books.iter().zip(&expected).enumerate() {
        let n: Vec<i64> = b.iter().map(|v| v.parse().expect("int")).collect();
        let tob = TopOfBook {
            bid_px: Price::from_raw(n[0]),
            bid_qty: Qty::from_raw(n[1]),
            ask_px: Price::from_raw(n[2]),
            ask_qty: Qty::from_raw(n[3]),
        };
        let got = state.update(tob);
        if want[0].is_empty() {
            assert_eq!(got, None, "row {i}: Python has no features");
            continue;
        }
        let want: Vec<f64> = want.iter().map(|v| v.parse().expect("float")).collect();
        let feats = got.unwrap_or_else(|| panic!("row {i}: Rust has no features"));
        for (k, (g, w)) in feats.iter().zip(&want[..N_FEATURES]).enumerate() {
            assert!(close(*g, *w), "row {i} feature {k}: rust {g} python {w}");
        }
        let signal = model
            .predict(&feats)
            .expect("finite features give a signal");
        for (k, (g, w)) in signal.probs.iter().zip(&want[N_FEATURES..]).enumerate() {
            assert!(close(*g, *w), "row {i} prob {k}: rust {g} python {w}");
        }
        compared += 1;
    }
    assert!(compared > 700, "fixture compared only {compared} rows");
}

#[test]
fn refuses_artifact_whose_hash_is_not_the_registry_hash() {
    let bytes = model_bytes();
    let wrong = "0".repeat(64);
    assert!(matches!(
        LinearModel::load(&bytes, &wrong),
        Err(LoadError::HashMismatch { .. })
    ));
    // A tampered artifact fails even though it is still valid JSON.
    let tampered = String::from_utf8(bytes)
        .expect("utf8")
        .replacen("0.", "1.", 1);
    assert!(matches!(
        LinearModel::load(tampered.as_bytes(), &fixture("model.sha256")),
        Err(LoadError::HashMismatch { .. })
    ));
}

#[test]
fn refuses_artifact_for_another_feature_set() {
    let other = String::from_utf8(model_bytes())
        .expect("utf8")
        .replace("tob-v1", "tob-v0");
    let hash = qc_inference::linear::sha256_hex(other.as_bytes());
    assert_eq!(
        LinearModel::load(other.as_bytes(), &hash).map(|_| ()),
        Err(LoadError::Incompatible("feature set".into()))
    );
}

#[test]
fn nan_or_missing_features_mean_no_signal() {
    let model = LinearModel::load(&model_bytes(), &fixture("model.sha256")).expect("load");
    assert!(model.predict(&[0.1, 0.2, 0.3, 1.0]).is_some());
    assert_eq!(model.predict(&[0.1, f64::NAN, 0.3, 1.0]), None);
    assert_eq!(model.predict(&[0.1, f64::INFINITY, 0.3, 1.0]), None);
    assert_eq!(model.predict(&[0.1, 0.2, 0.3]), None);
    assert_eq!(model.predict(&[]), None);
}
