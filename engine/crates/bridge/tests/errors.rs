//! Unknown op, bad protocol version, and a malformed decimal all fail as a
//! protocol-level error (`"ok":false`), never as a guessed default and never
//! as a business `IntentResult`.

mod support;

use qc_bridge::handle_line;
use serde_json::Value;

fn call(engine: &mut qc_bridge::BridgeEngine, line: &str) -> Value {
    let (response, _) = handle_line(engine, line);
    serde_json::from_str(&response).unwrap()
}

#[test]
fn unknown_op_is_an_error() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let resp = call(&mut engine, r#"{"v":1,"id":"1","op":"cancel_everything"}"#);
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "unknown_op");
    assert_eq!(resp["id"], "1");
}

#[test]
fn wrong_version_is_an_error_and_never_processed() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let resp = call(&mut engine, r#"{"v":2,"id":"1","op":"status"}"#);
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "bad_version");
    assert!(resp.get("status").is_none());
}

#[test]
fn missing_version_is_an_error() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let resp = call(&mut engine, r#"{"id":"1","op":"status"}"#);
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "bad_version");
}

#[test]
fn bad_decimal_in_an_order_intent_is_an_error_not_a_rejection() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let line = r#"{"v":1,"id":"1","op":"submit_order_intent","intent":
        {"request_id":"dr-1","instrument":"1","side":"buy","qty":"not-a-decimal",
         "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#;
    let resp = call(&mut engine, line);
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "invalid_fields");
    // Not a business decision: there is no IntentResult to check "accepted" on.
    assert!(resp.get("result").is_none());
}

#[test]
fn bad_decimal_in_a_limit_price_is_also_an_error() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let line = r#"{"v":1,"id":"1","op":"submit_order_intent","intent":
        {"request_id":"dr-1","instrument":"1","side":"buy","qty":"0.001",
         "limit_price":"1e10","time_in_force":"gtc","reason":"test"}}"#;
    let resp = call(&mut engine, line);
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "invalid_fields");
}

#[test]
fn unknown_instrument_is_an_error() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let line = r#"{"v":1,"id":"1","op":"submit_order_intent","intent":
        {"request_id":"dr-1","instrument":"999","side":"buy","qty":"0.001",
         "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#;
    let resp = call(&mut engine, line);
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "invalid_fields");
}

#[test]
fn malformed_json_line_is_still_a_single_error_line() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let resp = call(&mut engine, "{not json");
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "invalid_json");
}
