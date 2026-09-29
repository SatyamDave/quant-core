//! Protocol v1 wire format: one JSON object per line in, one per line out.
//! Parsing and dispatch live here so [`handle_line`] is the single place that
//! decides what is a protocol error versus a business decision (an
//! `IntentResult`), which is exactly what schema conformance tests check.

use qc_core::{ClientOrderId, OrderEvent, OrderType, Price, Qty, Side, Timestamp, VenueOrderId};
use qc_oms::VenueOrder;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::engine::{BridgeEngine, VenueMode};

const PROTOCOL_VERSION: u64 = 1;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum WireSide {
    Buy,
    Sell,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "lowercase")]
enum WireTimeInForce {
    Ioc,
    Gtc,
}

/// `order_intent.schema.json`. `qty` and `limit_price` reuse
/// `qc_core::Price`/`Qty`'s own `Deserialize`, so a malformed decimal string
/// fails here with the same message `qc_core` would give a config file.
#[derive(Debug, Deserialize)]
struct WireOrderIntent {
    // Bound into the approval payload (protocol v1.1) when accepted in
    // `external` venue mode; otherwise carried only for the caller's own
    // correlation.
    request_id: String,
    instrument: String,
    side: WireSide,
    qty: Qty,
    limit_price: Price,
    time_in_force: WireTimeInForce,
    #[allow(dead_code)]
    reason: String,
}

/// `order_execution.schema.json`'s `event` values.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum WireExecutionEvent {
    Accepted,
    Rejected,
    PartiallyFilled,
    Filled,
    Canceled,
    CancelRejected,
}

/// `order_execution.schema.json`. `qty`/`price`/`venue_order_id` are only
/// required for the event kinds that need them; that is checked in
/// [`execution_to_order_event`], not here, so a missing-but-not-needed field
/// never becomes a spurious protocol error.
#[derive(Debug, Deserialize)]
struct WireExecution {
    client_order_id: u64,
    event: WireExecutionEvent,
    qty: Option<Qty>,
    price: Option<Price>,
    venue_order_id: Option<String>,
    ts_ns: u64,
    #[allow(dead_code)]
    reason: Option<String>,
}

/// Broker order IDs are opaque strings on a real venue;
/// `qc_core::VenueOrderId` is a `u64` newtype today (the mapping is
/// UNVERIFIED until #26). Until then this is the one place that assumes the
/// wire's `venue_order_id` is a decimal integer string.
fn parse_venue_order_id(s: &str) -> Result<VenueOrderId, String> {
    s.parse::<u64>()
        .map(VenueOrderId)
        .map_err(|_| format!("venue_order_id {s:?} must be a decimal integer (see issue #26)"))
}

fn execution_to_order_event(exec: &WireExecution) -> Result<(OrderEvent, u64), String> {
    let client_id = ClientOrderId(exec.client_order_id);
    let ts = Timestamp(exec.ts_ns);
    let event = match exec.event {
        WireExecutionEvent::Accepted => {
            let Some(venue_order_id) = exec.venue_order_id.as_deref() else {
                return Err("\"venue_order_id\" is required for an \"accepted\" execution".into());
            };
            OrderEvent::Accepted {
                client_id,
                venue_id: parse_venue_order_id(venue_order_id)?,
                ts,
            }
        }
        WireExecutionEvent::Rejected => OrderEvent::Rejected { client_id, ts },
        WireExecutionEvent::PartiallyFilled | WireExecutionEvent::Filled => {
            // Both wire event names map onto `OrderEvent::Filled`: the OMS's
            // own cumulative-quantity arithmetic decides partial vs. final,
            // rather than trusting the label the report claims.
            let (Some(qty), Some(price)) = (exec.qty, exec.price) else {
                return Err("\"qty\" and \"price\" are required for a fill execution".into());
            };
            // A fill qty/price is a magnitude, never signed: `Qty`/`Price`
            // parse a leading '-' without complaint (it means something else
            // in other contexts, e.g. a short position), so this is the one
            // place that must refuse it. The OMS's own overfill check already
            // happens to reject a non-positive qty, but nothing upstream
            // guards price, and an unchecked negative price flips the sign of
            // the cash delta `apply_order_event` computes -- corrupting the
            // exact bookkeeping the max-daily-loss kill switch reads.
            if qty <= Qty::ZERO || price <= Price::ZERO {
                return Err("\"qty\" and \"price\" must be positive for a fill execution".into());
            }
            OrderEvent::Filled {
                client_id,
                price,
                qty,
                ts,
            }
        }
        WireExecutionEvent::Canceled => OrderEvent::Canceled { client_id, ts },
        WireExecutionEvent::CancelRejected => OrderEvent::CancelRejected { client_id, ts },
    };
    Ok((event, exec.ts_ns))
}

#[derive(Debug, Deserialize)]
struct WireNoTrade {
    #[allow(dead_code)]
    request_id: String,
    #[allow(dead_code)]
    reason: String,
}

/// `reconcile.schema.json`'s `venue.orders[]` (protocol v1.2): what the
/// gateway saw at the real venue, in the same shape `qc_oms::VenueOrder`
/// wants, plus the wire's own decimal-string quantity/state spellings.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum WireVenueOrderState {
    Open,
    Filled,
    Canceled,
    Rejected,
}

#[derive(Debug, Deserialize)]
struct WireVenueOrder {
    client_order_id: Option<u64>,
    venue_order_id: String,
    state: WireVenueOrderState,
    filled_qty: Qty,
}

#[derive(Debug, Deserialize)]
struct WireVenue {
    orders: Vec<WireVenueOrder>,
    position: Qty,
    #[allow(dead_code)]
    // accepted but not (yet) compared -- see `BridgeEngine::reconcile_with_venue`
    cash: Option<Price>,
}

fn wire_venue_order_to_oms(v: &WireVenueOrder) -> Result<VenueOrder, String> {
    Ok(VenueOrder {
        venue_id: parse_venue_order_id(&v.venue_order_id)?,
        client_id: v.client_order_id.map(ClientOrderId),
        open: matches!(v.state, WireVenueOrderState::Open),
        filled: v.filled_qty,
    })
}

fn ok_response(id: &str, extra: Map<String, Value>) -> String {
    let mut obj = Map::new();
    obj.insert("v".to_owned(), json!(PROTOCOL_VERSION));
    obj.insert("id".to_owned(), json!(id));
    obj.insert("ok".to_owned(), json!(true));
    for (k, v) in extra {
        obj.insert(k, v);
    }
    Value::Object(obj).to_string()
}

fn error_response(id: &str, code: &str, message: impl Into<String>) -> String {
    json!({
        "v": PROTOCOL_VERSION,
        "id": id,
        "ok": false,
        "error": {"code": code, "message": message.into()},
    })
    .to_string()
}

fn extra(pairs: impl IntoIterator<Item = (&'static str, Value)>) -> Map<String, Value> {
    pairs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect()
}

/// Parses one request line, dispatches it against `engine`, and returns the
/// exactly-one response line. The second element is `true` only for
/// `shutdown`, telling the caller's read loop to stop after this line.
#[must_use]
pub fn handle_line(engine: &mut BridgeEngine, line: &str) -> (String, bool) {
    let value: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => {
            return (
                error_response("unknown", "invalid_json", e.to_string()),
                false,
            );
        }
    };
    let id = value
        .get("id")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_owned();
    if value.get("v").and_then(Value::as_u64) != Some(PROTOCOL_VERSION) {
        return (
            error_response(&id, "bad_version", "\"v\" must be the integer 1"),
            false,
        );
    }
    let Some(op) = value.get("op").and_then(Value::as_str) else {
        return (
            error_response(&id, "invalid_fields", "\"op\" is required"),
            false,
        );
    };
    // Protocol v1.2, issue #44: polled on every op (not just every record),
    // so an out-of-band trigger (`--kill-file`) or an externally-engaged
    // `KillSwitch` is observed even by a bare `status`/`hello` call.
    engine.poll_kill_file();
    engine.check_kill_switch();
    match op {
        "next_decision_request" => {
            let dr = engine.next_decision_request().unwrap_or(Value::Null);
            (ok_response(&id, extra([("decision_request", dr)])), false)
        }
        "submit_order_intent" => (submit_order_intent(engine, &id, &value), false),
        "no_trade" => match serde_json::from_value::<WireNoTrade>(value.clone()) {
            Ok(_) => (ok_response(&id, Map::new()), false),
            Err(e) => (error_response(&id, "invalid_fields", e.to_string()), false),
        },
        "status" => (
            ok_response(&id, extra([("status", engine.status())])),
            false,
        ),
        "hello" => (ok_response(&id, extra([("hello", engine.hello())])), false),
        "report_execution" => (report_execution(engine, &id, &value), false),
        "kill" => (kill(engine, &id, &value), false),
        "cancel_order_intent" => (cancel_order_intent(engine, &id, &value), false),
        "reconcile" => (reconcile(engine, &id, &value), false),
        "drain_halt_cancels" => (
            ok_response(
                &id,
                extra([("pending_cancels", Value::Array(engine.pending_cancels()))]),
            ),
            false,
        ),
        "shutdown" => (ok_response(&id, Map::new()), true),
        other => (
            error_response(&id, "unknown_op", format!("unknown op {other:?}")),
            false,
        ),
    }
}

#[derive(Debug, Deserialize)]
struct WireKill {
    reason: String,
}

/// `kill` (protocol v1.2, issue #44): always succeeds — engaging an
/// already-engaged switch is a no-op, not an error.
fn kill(engine: &mut BridgeEngine, id: &str, value: &Value) -> String {
    let kill: WireKill = match serde_json::from_value(value.clone()) {
        Ok(k) => k,
        Err(e) => return error_response(id, "invalid_fields", e.to_string()),
    };
    engine.kill(&kill.reason);
    ok_response(id, Map::new())
}

#[derive(Debug, Deserialize)]
struct WireCancelOrderIntent {
    client_order_id: u64,
    #[allow(dead_code)]
    reason: String,
}

/// `cancel_order_intent` (protocol v1.2): an unknown or already-terminal id
/// is a protocol error, not a business rejection — root rule 12, fail
/// closed on anything unclear rather than silently no-op.
fn cancel_order_intent(engine: &mut BridgeEngine, id: &str, value: &Value) -> String {
    let req: WireCancelOrderIntent = match serde_json::from_value(value.clone()) {
        Ok(r) => r,
        Err(e) => return error_response(id, "invalid_fields", e.to_string()),
    };
    match engine.cancel_order_intent(req.client_order_id, &req.reason) {
        Ok(result) => ok_response(id, extra([("result", result)])),
        Err(message) => error_response(id, "invalid_cancel", message),
    }
}

/// `reconcile` (protocol v1.2, issue #45): the gateway's view of venue
/// orders/position/cash, compared against the OMS.
fn reconcile(engine: &mut BridgeEngine, id: &str, value: &Value) -> String {
    let Some(venue) = value.get("venue") else {
        return error_response(id, "invalid_fields", "\"venue\" is required");
    };
    let venue: WireVenue = match serde_json::from_value(venue.clone()) {
        Ok(v) => v,
        Err(e) => return error_response(id, "invalid_fields", e.to_string()),
    };
    let mut venue_orders = Vec::with_capacity(venue.orders.len());
    for o in &venue.orders {
        match wire_venue_order_to_oms(o) {
            Ok(v) => venue_orders.push(v),
            Err(message) => return error_response(id, "invalid_fields", message),
        }
    }
    let result = engine.reconcile_with_venue(&venue_orders, venue.position);
    ok_response(id, extra([("reconcile", result)]))
}

fn submit_order_intent(engine: &mut BridgeEngine, id: &str, value: &Value) -> String {
    let Some(intent) = value.get("intent") else {
        return error_response(id, "invalid_fields", "\"intent\" is required");
    };
    let intent: WireOrderIntent = match serde_json::from_value(intent.clone()) {
        Ok(i) => i,
        Err(e) => return error_response(id, "invalid_fields", e.to_string()),
    };
    if intent.instrument != engine.instrument_label() {
        return error_response(
            id,
            "invalid_fields",
            format!("unknown instrument {:?}", intent.instrument),
        );
    }
    let side = match intent.side {
        WireSide::Buy => Side::Buy,
        WireSide::Sell => Side::Sell,
    };
    let tif = match intent.time_in_force {
        WireTimeInForce::Ioc => OrderType::Ioc,
        WireTimeInForce::Gtc => OrderType::Limit,
    };
    let result = engine.submit_order_intent(
        &intent.request_id,
        side,
        intent.qty,
        intent.limit_price,
        tif,
    );
    ok_response(id, extra([("result", result)]))
}

/// `report_execution` (protocol v1.1, issue #34): only meaningful in
/// `external` venue mode, where the TS gateway — not `SimVenue` — is the
/// source of truth for what happened to an order. Refused as a protocol
/// error in `sim` mode, rather than silently accepted and ignored.
fn report_execution(engine: &mut BridgeEngine, id: &str, value: &Value) -> String {
    if engine.venue_mode() != VenueMode::External {
        return error_response(
            id,
            "invalid_mode",
            "report_execution is only valid in --venue external",
        );
    }
    let Some(execution) = value.get("execution") else {
        return error_response(id, "invalid_fields", "\"execution\" is required");
    };
    let exec: WireExecution = match serde_json::from_value(execution.clone()) {
        Ok(e) => e,
        Err(e) => return error_response(id, "invalid_fields", e.to_string()),
    };
    let (event, ts_ns) = match execution_to_order_event(&exec) {
        Ok(pair) => pair,
        Err(message) => return error_response(id, "invalid_fields", message),
    };
    match engine.report_execution(event, ts_ns) {
        Ok(()) => ok_response(id, Map::new()),
        Err(e) => error_response(id, "illegal_transition", format!("{e:?}")),
    }
}
