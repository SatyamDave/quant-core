//! With `--model`/`--model-sha256` the bridge loads a real `ml/export`
//! artifact and reports a non-null signal once features warm up; without
//! them, signal stays null (checked in `protocol_roundtrip.rs`).

mod support;

use std::io::Write as _;

use qc_bridge::{BridgeEngine, LoadedModel, VenueMode};
use qc_inference::linear::{LinearModel, sha256_hex};
use qc_risk::KillSwitch;

fn model_fixture_bytes() -> Vec<u8> {
    std::fs::read(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../ml/tests/fixtures/model.json"
    ))
    .expect("ml/tests/fixtures/model.json is committed")
}

#[test]
fn decision_request_carries_a_signal_once_a_model_is_configured_and_warmed_up() {
    let bytes = model_fixture_bytes();
    let sha256 = sha256_hex(&bytes);
    let model = LinearModel::load(&bytes, &sha256).expect("fixture hash matches its own bytes");
    let loaded = LoadedModel {
        model: Box::new(model),
        sha256: sha256.clone(),
    };

    let records =
        BridgeEngine::load_recording(&support::sample_day_csv()).expect("sample day parses");
    let mut engine = BridgeEngine::new(
        records,
        support::limits(),
        KillSwitch::new(),
        "0.1".parse().unwrap(),
        1,
        Some(loaded),
        VenueMode::Sim,
    );

    let mut seen_signal = false;
    for _ in 0..200 {
        let Some(dr) = engine.next_decision_request() else {
            break;
        };
        if let Some(signal) = dr.get("signal").filter(|s| !s.is_null()) {
            assert_eq!(signal["model_sha256"], sha256);
            let direction = signal["direction"].as_str().unwrap();
            assert!(["up", "flat", "down"].contains(&direction));
            let probs = signal["probs"].as_array().unwrap();
            assert_eq!(probs.len(), 3);
            let sum: f64 = probs.iter().map(|p| p.as_f64().unwrap()).sum();
            assert!((sum - 1.0).abs() < 1e-6, "probabilities sum to 1: {sum}");
            seen_signal = true;
            break;
        }
    }
    assert!(
        seen_signal,
        "expected a non-null signal within 200 decisions once features warm up"
    );
}

#[test]
fn a_hash_that_does_not_match_the_bytes_fails_to_load() {
    let bytes = model_fixture_bytes();
    let wrong = "0".repeat(64);
    assert!(LinearModel::load(&bytes, &wrong).is_err());
}

#[test]
fn the_compiled_binary_loads_a_model_from_the_command_line() {
    let recording = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../tests/replay/sample_day.csv"
    );
    let limits = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../config/limits/default.toml"
    );
    let model_path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../ml/tests/fixtures/model.json"
    );
    let sha256 = sha256_hex(&model_fixture_bytes());

    let mut child = std::process::Command::new(env!("CARGO_BIN_EXE_qc-bridge"))
        .arg(recording)
        .arg("--limits")
        .arg(limits)
        .arg("--decide-every")
        .arg("1")
        .arg("--model")
        .arg(model_path)
        .arg("--model-sha256")
        .arg(&sha256)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .expect("qc-bridge starts");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"{\"v\":1,\"id\":\"1\",\"op\":\"shutdown\"}\n")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(output.status.success(), "{output:?}");
    let line = String::from_utf8(output.stdout).unwrap();
    let value: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
    assert_eq!(
        value["ok"], true,
        "a bad --model-sha256 would have failed at startup instead"
    );
}
