//! Every JSON the bridge emits validates against `schemas/decision/v1`
//! (ADR-0040's contract between `qc-bridge` and the TypeScript agent
//! service). The registry is built once from the committed files so `$ref`s
//! between them resolve offline, with no HTTP or filesystem schema resolver.

mod support;

use jsonschema::{Registry, Validator};
use qc_bridge::handle_line;
use serde_json::Value;

fn schema(text: &str) -> Value {
    serde_json::from_str(text).expect("committed schema file is valid JSON")
}

/// All nine v1(.1) schemas, keyed by the `$id` each one declares, so `$ref`s
/// such as `order_intent.schema.json` resolve relative to the referencing
/// schema's own `$id` without contacting a network or the filesystem.
fn registry() -> Registry<'static> {
    let docs = [
        schema(include_str!(
            "../../../../schemas/decision/v1/bridge_request.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/bridge_response.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/decision.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/decision_request.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/intent_result.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/order_intent.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/order_execution.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/approval.schema.json"
        )),
        schema(include_str!(
            "../../../../schemas/decision/v1/hello.schema.json"
        )),
    ];
    let pairs = docs.into_iter().map(|doc| {
        let id = doc["$id"]
            .as_str()
            .expect("every v1 schema declares $id")
            .to_owned();
        (id, doc)
    });
    Registry::new()
        .extend(pairs)
        .expect("every $id is a valid URI")
        .prepare()
        .expect("the nine v1(.1) schemas resolve against each other with no external fetch")
}

fn validator_for<'a>(registry: &'a Registry<'a>, doc: &Value) -> Validator {
    jsonschema::options()
        .with_registry(registry)
        .build(doc)
        .expect("compiles against the 2020-12 draft the schemas declare")
}

#[test]
fn decision_request_conforms_to_its_schema() {
    let registry = registry();
    let decision_request_schema = schema(include_str!(
        "../../../../schemas/decision/v1/decision_request.schema.json"
    ));
    let validator = validator_for(&registry, &decision_request_schema);

    let mut engine = support::engine_from_with_limits(
        &support::sample_day_csv(),
        5,
        support::sample_day_limits(),
    );
    let mut checked = 0;
    while let Some(dr) = engine.next_decision_request() {
        assert!(
            validator.is_valid(&dr),
            "decision_request failed schema: {:?}\n{dr:#}",
            validator.iter_errors(&dr).collect::<Vec<_>>()
        );
        checked += 1;
        if checked >= 20 {
            break;
        }
    }
    assert!(
        checked > 0,
        "the sample day should yield at least one decision request"
    );
}

#[test]
fn bridge_response_and_intent_result_conform_for_every_op() {
    let registry = registry();
    let response_schema = schema(include_str!(
        "../../../../schemas/decision/v1/bridge_response.schema.json"
    ));
    let intent_result_schema = schema(include_str!(
        "../../../../schemas/decision/v1/intent_result.schema.json"
    ));
    let response_validator = validator_for(&registry, &response_schema);
    let intent_result_validator = validator_for(&registry, &intent_result_schema);

    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let lines = [
        r#"{"v":1,"id":"1","op":"status"}"#,
        r#"{"v":1,"id":"2","op":"next_decision_request"}"#,
        r#"{"v":1,"id":"3","op":"submit_order_intent","intent":
            {"request_id":"dr-1","instrument":"1","side":"buy","qty":"0.001",
             "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#,
        r#"{"v":1,"id":"4","op":"no_trade","request_id":"dr-2","reason":"flat"}"#,
        r#"{"v":1,"id":"6","op":"hello"}"#,
        r#"{"v":1,"id":"7","op":"cancel_order_intent","client_order_id":1,"reason":"test"}"#,
        r#"{"v":1,"id":"8","op":"reconcile","venue":{"orders":[],"position":"0","cash":null}}"#,
        r#"{"v":1,"id":"9","op":"kill","reason":"test"}"#,
        r#"{"v":1,"id":"10","op":"drain_halt_cancels"}"#,
        r#"{"v":1,"id":"5","op":"shutdown"}"#,
    ];
    for line in lines {
        let (response, _) = handle_line(&mut engine, line);
        let value: Value = serde_json::from_str(&response).unwrap();
        assert!(
            response_validator.is_valid(&value),
            "response failed schema: {:?}\n{value:#}",
            response_validator.iter_errors(&value).collect::<Vec<_>>()
        );
        if let Some(result) = value.get("result") {
            assert!(
                intent_result_validator.is_valid(result),
                "result failed schema: {result:#}"
            );
        }
    }
}

#[test]
fn external_mode_approval_and_report_execution_conform() {
    let registry = registry();
    let response_schema = schema(include_str!(
        "../../../../schemas/decision/v1/bridge_response.schema.json"
    ));
    let intent_result_schema = schema(include_str!(
        "../../../../schemas/decision/v1/intent_result.schema.json"
    ));
    let response_validator = validator_for(&registry, &response_schema);
    let intent_result_validator = validator_for(&registry, &intent_result_schema);

    let mut engine = support::engine_external_reconciled(&support::small_book_csv(), 1);
    assert!(
        engine.next_decision_request().is_some(),
        "book must sync before a submit"
    );
    let lines = [
        r#"{"v":1,"id":"1","op":"hello"}"#,
        r#"{"v":1,"id":"2","op":"submit_order_intent","intent":
            {"request_id":"dr-1","instrument":"1","side":"buy","qty":"0.001",
             "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#,
        r#"{"v":1,"id":"3","op":"report_execution","execution":
            {"client_order_id":1,"event":"accepted","venue_order_id":"1","ts_ns":1}}"#,
        // A halt (protocol v1.2.1) must expose a real, non-empty
        // `pending_cancels` too -- an empty array would pass the item schema
        // trivially, so this is what actually exercises its shape.
        r#"{"v":1,"id":"9","op":"kill","reason":"test"}"#,
        r#"{"v":1,"id":"10","op":"drain_halt_cancels"}"#,
    ];
    let mut saw_approval = false;
    let mut saw_pending_cancel = false;
    for line in lines {
        let (response, _) = handle_line(&mut engine, line);
        let value: Value = serde_json::from_str(&response).unwrap();
        assert!(
            response_validator.is_valid(&value),
            "response failed schema: {:?}\n{value:#}",
            response_validator.iter_errors(&value).collect::<Vec<_>>()
        );
        if let Some(result) = value.get("result") {
            assert!(
                intent_result_validator.is_valid(result),
                "result failed schema: {result:#}"
            );
            saw_approval |= result.get("approval").is_some();
        }
        if let Some(pending) = value.get("pending_cancels").and_then(Value::as_array) {
            saw_pending_cancel |= !pending.is_empty();
        }
    }
    assert!(
        saw_approval,
        "external mode must produce an approval to check"
    );
    assert!(
        saw_pending_cancel,
        "drain_halt_cancels must produce a real pending cancel to check its schema"
    );
}

#[test]
fn oversized_intent_result_still_conforms() {
    let registry = registry();
    let intent_result_schema = schema(include_str!(
        "../../../../schemas/decision/v1/intent_result.schema.json"
    ));
    let validator = validator_for(&registry, &intent_result_schema);

    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    let result = engine.submit_order_intent(
        "dr-1",
        qc_core::Side::Buy,
        "1000".parse().unwrap(),
        "99.8".parse().unwrap(),
        qc_core::OrderType::Limit,
    );
    assert_eq!(result["accepted"], false);
    assert!(validator.is_valid(&result), "{result:#}");
}
