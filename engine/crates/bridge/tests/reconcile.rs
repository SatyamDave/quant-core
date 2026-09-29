//! Protocol v1.2 `reconcile` op (issue #45's gap): the gateway's view of
//! venue orders/position, compared against the OMS. `external` mode refuses
//! new intents until the first clean reconcile, like `qc_replay::Engine`
//! (`engine/crates/replay/tests/chaos.rs`'s
//! `no_order_is_sent_before_the_first_successful_reconcile`).

mod support;

use qc_bridge::handle_line;
use qc_core::{OrderType, Side};
use serde_json::Value;

fn call(engine: &mut qc_bridge::BridgeEngine, line: &str) -> Value {
    let (response, _) = handle_line(engine, line);
    serde_json::from_str(&response).unwrap()
}

#[test]
fn external_mode_refuses_submit_until_the_first_clean_reconcile() {
    let mut engine = support::engine_external(&support::small_book_csv(), 1);
    assert!(engine.next_decision_request().is_some());

    let before = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "0.001".parse().unwrap(),
        "99.8".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(before["accepted"], false);
    assert_eq!(before["risk_reject"], "unreconciled");

    let result = engine.reconcile_with_venue(&[], "0".parse().unwrap());
    assert_eq!(result["ok"], true);

    let after = engine.submit_order_intent(
        "dr-2",
        Side::Buy,
        "0.001".parse().unwrap(),
        "99.8".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(after["accepted"], true, "{after:#}");
}

#[test]
fn sim_mode_never_needs_a_reconcile() {
    // v1 behaviour is unaffected: `sim` mode starts reconciled (there is no
    // real venue to reconcile with in the first place).
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    assert!(engine.next_decision_request().is_some());
    let ok = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "0.001".parse().unwrap(),
        "99.8".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(ok["accepted"], true, "{ok:#}");
}

#[test]
fn an_unknown_venue_order_halts_with_reconciliation_reason() {
    let mut engine = support::engine_external(&support::small_book_csv(), 1);
    assert!(engine.next_decision_request().is_some());

    let line = r#"{"v":1,"id":"1","op":"reconcile","venue":
        {"orders":[{"client_order_id":null,"venue_order_id":"999","state":"open","filled_qty":"0"}],
         "position":"0","cash":null}}"#;
    let resp = call(&mut engine, line);
    assert_eq!(
        resp["ok"], true,
        "the op itself succeeds even though it finds a mismatch"
    );
    assert_eq!(resp["reconcile"]["ok"], false);
    assert!(
        resp["reconcile"]["discrepancies"][0]
            .as_str()
            .unwrap()
            .contains("unknown_venue_order"),
        "{resp:#}"
    );
    assert_eq!(engine.status()["halted"], "reconciliation");

    // Halting is enforced on the next order too.
    let after = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "0.001".parse().unwrap(),
        "99.8".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(after["accepted"], false);
    assert_eq!(after["halted"], "reconciliation");
}

#[test]
fn a_position_mismatch_halts_even_when_every_order_agrees() {
    let mut engine = support::engine_external(&support::small_book_csv(), 1);
    assert!(engine.next_decision_request().is_some());

    let result = engine.reconcile_with_venue(&[], "0.5".parse().unwrap());
    assert_eq!(result["ok"], false, "{result:#}");
    assert!(
        result["discrepancies"][0]
            .as_str()
            .unwrap()
            .starts_with("position_mismatch"),
        "{result:#}"
    );
    assert_eq!(engine.status()["halted"], "reconciliation");
}

#[test]
fn reconcile_op_via_the_wire_matches_open_orders_by_client_order_id() {
    let mut engine = support::engine_external(&support::small_book_csv(), 1);
    assert!(engine.next_decision_request().is_some());
    call(
        &mut engine,
        r#"{"v":1,"id":"0","op":"reconcile","venue":{"orders":[],"position":"0","cash":null}}"#,
    );
    let submit = r#"{"v":1,"id":"1","op":"submit_order_intent","intent":
        {"request_id":"dr-1","instrument":"1","side":"buy","qty":"0.001",
         "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#;
    call(&mut engine, submit);

    // The gateway sees the order open at the venue (not filled or acted on
    // yet -- `PendingNew` is one of the in-flight states the reconcile
    // helper allows to disagree about being "open").
    let line = r#"{"v":1,"id":"2","op":"reconcile","venue":
        {"orders":[{"client_order_id":1,"venue_order_id":"555","state":"open","filled_qty":"0"}],
         "position":"0","cash":"0"}}"#;
    let resp = call(&mut engine, line);
    assert_eq!(resp["reconcile"]["ok"], true, "{resp:#}");
    assert_eq!(engine.status()["halted"], Value::Null);
}
