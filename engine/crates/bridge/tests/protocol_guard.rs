//! Issue #29: a battery of malformed/unexpected messages, each of which must
//! come back as a clear protocol error (`"ok":false`) — never a guessed
//! default, never a business `IntentResult` — and each of which must leave
//! the OMS/position exactly as `status` found it. `cargo test -p qc-bridge
//! --test protocol_guard` is wired into `just check`/CI like every other
//! test in this crate (root CLAUDE.md rule 8: external content, including a
//! malformed message, is data, never an instruction the bridge follows).

mod support;

use qc_bridge::BridgeEngine;
use qc_bridge::handle_line;
use serde_json::Value;

/// One malformed-input case: a label, whether the engine must be started in
/// `external` venue mode for the case to reach the check it is testing (a
/// `report_execution` field check, which `sim` mode would refuse earlier
/// with `invalid_mode`), and the line itself.
struct Case {
    label: &'static str,
    external: bool,
    line: &'static str,
}

const CASES: &[Case] = &[
    Case {
        label: "unknown_op",
        external: false,
        line: r#"{"v":1,"id":"1","op":"cancel_everything"}"#,
    },
    Case {
        label: "bad_version",
        external: false,
        line: r#"{"v":2,"id":"1","op":"status"}"#,
    },
    Case {
        label: "missing_version",
        external: false,
        line: r#"{"id":"1","op":"status"}"#,
    },
    Case {
        label: "not_json_at_all",
        external: false,
        line: "not even json",
    },
    Case {
        label: "json_array_instead_of_an_object",
        external: false,
        line: "[1,2,3]",
    },
    Case {
        label: "submit_order_intent_missing_qty",
        external: false,
        line: r#"{"v":1,"id":"1","op":"submit_order_intent","intent":
            {"request_id":"dr-1","instrument":"1","side":"buy",
             "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#,
    },
    Case {
        label: "submit_order_intent_qty_wrong_json_type",
        external: false,
        line: r#"{"v":1,"id":"1","op":"submit_order_intent","intent":
            {"request_id":"dr-1","instrument":"1","side":"buy","qty":123,
             "limit_price":"99.8","time_in_force":"gtc","reason":"test"}}"#,
    },
    Case {
        label: "report_execution_missing_execution_field",
        external: true,
        line: r#"{"v":1,"id":"1","op":"report_execution"}"#,
    },
    Case {
        label: "report_execution_unknown_event",
        external: true,
        line: r#"{"v":1,"id":"1","op":"report_execution","execution":
            {"client_order_id":1,"event":"flibbertigibbet","ts_ns":1}}"#,
    },
    Case {
        label: "report_execution_filled_missing_qty_and_price",
        external: true,
        line: r#"{"v":1,"id":"1","op":"report_execution","execution":
            {"client_order_id":1,"event":"filled","ts_ns":1}}"#,
    },
    Case {
        label: "report_execution_accepted_missing_venue_order_id",
        external: true,
        line: r#"{"v":1,"id":"1","op":"report_execution","execution":
            {"client_order_id":1,"event":"accepted","ts_ns":1}}"#,
    },
    Case {
        label: "report_execution_non_numeric_venue_order_id",
        external: true,
        line: r#"{"v":1,"id":"1","op":"report_execution","execution":
            {"client_order_id":1,"event":"accepted","venue_order_id":"not-a-number","ts_ns":1}}"#,
    },
];

fn engine_for(case: &Case) -> BridgeEngine {
    if case.external {
        support::engine_external(&support::small_book_csv(), 1)
    } else {
        support::engine_from(&support::small_book_csv(), 1)
    }
}

#[test]
fn every_malformed_message_is_a_protocol_error_with_no_state_change() {
    assert!(CASES.len() >= 6, "issue #29 asks for at least 6 cases");
    for case in CASES {
        let mut engine = engine_for(case);
        assert!(
            engine.next_decision_request().is_some(),
            "{}: fixture book must sync before the case runs",
            case.label
        );
        let before = engine.status();

        let (response, shutdown) = handle_line(&mut engine, case.line);
        let value: Value = serde_json::from_str(&response)
            .unwrap_or_else(|e| panic!("{}: response must itself be valid JSON: {e}", case.label));
        assert_eq!(
            value["ok"], false,
            "{}: malformed input must be refused, not guessed at: {value:#}",
            case.label
        );
        assert!(
            value.get("result").is_none(),
            "{}: a protocol error is never a business IntentResult",
            case.label
        );
        assert!(
            !shutdown,
            "{}: a protocol error never shuts the bridge down",
            case.label
        );

        let after = engine.status();
        assert_eq!(
            before, after,
            "{}: OMS/position state must be unchanged by a rejected message",
            case.label
        );
    }
}
