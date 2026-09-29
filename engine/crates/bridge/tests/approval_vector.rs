//! `schemas/decision/v1/approval-test-vector.json` is a fixed, non-secret
//! keypair plus one real payload/signature pair, exported so the TypeScript
//! gateway can test its own approval verification against real Rust output
//! (issue #34). This test is what keeps that committed fixture honest: it
//! recomputes the same vector from the same inputs on every run and checks
//! the file has not drifted from what this crate's own `approval::mint`
//! actually produces, then round-trips it through `approval::verify`.

use ed25519_dalek::SigningKey;
use qc_bridge::approval::{self, OrderFields, VerifyError};
use serde_json::Value;

fn vector() -> Value {
    let text = include_str!("../../../../schemas/decision/v1/approval-test-vector.json");
    serde_json::from_str(text).expect("committed test vector is valid JSON")
}

fn seed_from_hex(hex: &str) -> [u8; 32] {
    let mut seed = [0u8; 32];
    for (i, chunk) in hex.as_bytes().chunks(2).enumerate() {
        let byte = std::str::from_utf8(chunk).unwrap();
        seed[i] = u8::from_str_radix(byte, 16).expect("committed seed_hex is valid hex");
    }
    seed
}

#[test]
fn committed_vector_matches_a_fresh_mint_from_the_same_inputs() {
    let vector = vector();
    let seed = seed_from_hex(vector["seed_hex"].as_str().unwrap());
    let key = SigningKey::from_bytes(&seed);
    assert_eq!(
        approval::encode_public_key(&key.verifying_key()),
        vector["public_key_base64"].as_str().unwrap(),
        "committed public key must match the one the fixed seed derives"
    );

    let order_json = &vector["order"];
    let order = OrderFields {
        client_order_id: order_json["client_order_id"].as_u64().unwrap(),
        request_id: order_json["request_id"].as_str().unwrap(),
        instrument: order_json["instrument"].as_str().unwrap(),
        side: "buy",
        qty: order_json["qty"].as_str().unwrap().parse().unwrap(),
        limit_price: order_json["limit_price"].as_str().unwrap().parse().unwrap(),
        time_in_force: "gtc",
    };
    let decision_ts_ns = vector["decision_ts_ns"].as_u64().unwrap();

    let fresh = approval::mint(&key, &order, decision_ts_ns);
    assert_eq!(
        fresh, vector["approval"],
        "approval-test-vector.json is stale: regenerate it from approval::mint with these exact inputs"
    );

    let now_ns = decision_ts_ns + 1;
    approval::verify(&key.verifying_key(), &fresh, &order, now_ns)
        .expect("the vector's own approval must verify against the vector's own public key");
}

#[test]
fn committed_vector_rejects_a_public_key_mismatch() {
    let vector = vector();
    let other_key = SigningKey::from_bytes(&[9u8; 32]);
    let order_json = &vector["order"];
    let order = OrderFields {
        client_order_id: order_json["client_order_id"].as_u64().unwrap(),
        request_id: order_json["request_id"].as_str().unwrap(),
        instrument: order_json["instrument"].as_str().unwrap(),
        side: "buy",
        qty: order_json["qty"].as_str().unwrap().parse().unwrap(),
        limit_price: order_json["limit_price"].as_str().unwrap().parse().unwrap(),
        time_in_force: "gtc",
    };
    let now_ns = vector["decision_ts_ns"].as_u64().unwrap() + 1;
    assert_eq!(
        approval::verify(
            &other_key.verifying_key(),
            &vector["approval"],
            &order,
            now_ns
        ),
        Err(VerifyError::BadSignature)
    );
}
