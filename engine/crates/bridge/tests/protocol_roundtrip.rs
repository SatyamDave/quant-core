//! Protocol v1 round trip: every op in `agentic-spec.md`'s handoff protocol
//! gets a well-formed response with the `id` echoed back.

mod support;

use qc_bridge::handle_line;
use serde_json::Value;

fn call(engine: &mut qc_bridge::BridgeEngine, line: &str) -> Value {
    let (response, _shutdown) = handle_line(engine, line);
    serde_json::from_str(&response).expect("every response is valid JSON")
}

#[test]
fn next_decision_request_returns_a_schema_shaped_request_then_null_when_exhausted() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let first = call(
        &mut engine,
        r#"{"v":1,"id":"1","op":"next_decision_request"}"#,
    );
    assert_eq!(first["v"], 1);
    assert_eq!(first["id"], "1");
    assert_eq!(first["ok"], true);
    let dr = &first["decision_request"];
    assert_eq!(dr["request_id"], "dr-1");
    assert_eq!(
        dr["signal"],
        Value::Null,
        "no model configured means no signal"
    );
    assert!(dr["features"].is_object());
    assert_eq!(
        dr["allowed_actions"],
        serde_json::json!(["buy", "sell", "no_trade"])
    );

    // The two-record fixture (one snapshot, one delta) yields one decision
    // request per record at decide_every: 1, then the feed is exhausted.
    let second = call(
        &mut engine,
        r#"{"v":1,"id":"2","op":"next_decision_request"}"#,
    );
    assert_eq!(second["decision_request"]["request_id"], "dr-2");
    let third = call(
        &mut engine,
        r#"{"v":1,"id":"3","op":"next_decision_request"}"#,
    );
    assert_eq!(third["decision_request"], Value::Null);
}

#[test]
fn submit_order_intent_accepts_a_valid_order_and_reports_the_client_order_id() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let _ = call(
        &mut engine,
        r#"{"v":1,"id":"1","op":"next_decision_request"}"#,
    );
    let submit = r#"{"v":1,"id":"2","op":"submit_order_intent","intent":
        {"request_id":"dr-1","instrument":"1","side":"buy","qty":"0.001",
         "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#;
    let resp = call(&mut engine, submit);
    assert_eq!(resp["ok"], true);
    assert_eq!(resp["result"]["accepted"], true);
    assert_eq!(resp["result"]["client_order_id"], 1);
}

#[test]
fn no_trade_is_acknowledged() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let resp = call(
        &mut engine,
        r#"{"v":1,"id":"1","op":"no_trade","request_id":"dr-1","reason":"flat market"}"#,
    );
    assert_eq!(resp["ok"], true);
    assert_eq!(resp["id"], "1");
}

#[test]
fn status_reports_position_and_limits() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let resp = call(&mut engine, r#"{"v":1,"id":"1","op":"status"}"#);
    assert_eq!(resp["ok"], true);
    assert_eq!(resp["status"]["position"], "0.00000000");
    assert_eq!(resp["status"]["halted"], Value::Null);
    assert!(resp["status"]["limits"]["max_position"].is_string());
}

#[test]
fn shutdown_acks_and_tells_the_caller_to_stop_reading() {
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let (response, shutdown) = handle_line(&mut engine, r#"{"v":1,"id":"1","op":"shutdown"}"#);
    let value: Value = serde_json::from_str(&response).unwrap();
    assert_eq!(value["ok"], true);
    assert!(shutdown);
}
