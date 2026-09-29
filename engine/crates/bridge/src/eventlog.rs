//! Structured stderr event log (protocol v1.2, issue #64/#44): one compact
//! JSON object per line, matching `scripts/ops/alerts.py`'s contract exactly
//! (see that module's docstring and `docs/runbooks/kill-switch.md`
//! "Alerting"). `ts_ns` is always the bridge's own market/sim clock, never a
//! wall-clock read (root rule: no wall clock). Stdout stays protocol-only;
//! this is the only thing besides `main`'s own fatal-startup message this
//! crate ever writes to stderr.

use serde_json::{Map, Value, json};

fn emit(event: &str, ts_ns: u64, fields: impl IntoIterator<Item = (&'static str, Value)>) {
    let mut obj = Map::new();
    obj.insert("ts_ns".to_owned(), json!(ts_ns));
    obj.insert("event".to_owned(), json!(event));
    for (k, v) in fields {
        obj.insert(k.to_owned(), v);
    }
    eprintln!("{}", Value::Object(obj));
}

/// `{"ts_ns","event":"halt","reason","detail"?}` — the vocabulary is
/// `HaltReason::as_str` (`kill_switch`, `max_daily_loss`, `reconciliation`,
/// `illegal_order_event`), already anticipated by `alerts.py`'s docstring.
pub fn halt(ts_ns: u64, reason: &str, detail: Option<&str>) {
    let mut fields: Vec<(&'static str, Value)> = vec![("reason", json!(reason))];
    if let Some(d) = detail {
        fields.push(("detail", json!(d)));
    }
    emit("halt", ts_ns, fields);
}

/// `{"ts_ns","event":"risk_reject","reason","instrument"}`, reusing
/// `risk_reject_code`'s exact vocabulary plus this crate's own additions
/// (`unreconciled`, `outside_trading_hours`, `invalid_tick_size`,
/// `invalid_qty_step`) — one string vocabulary, never a second one invented
/// for logging (`alerts.py`'s own docstring states this convention).
pub fn risk_reject(ts_ns: u64, reason: &str, instrument: &str) {
    emit(
        "risk_reject",
        ts_ns,
        [("reason", json!(reason)), ("instrument", json!(instrument))],
    );
}

/// `{"ts_ns","event":"kill_switch_engaged","source":"file"|"op"}` (issue
/// #44): a future, more immediate signal than waiting for the `halt` line,
/// per `alerts.py`'s `check_kill_switch_engaged`.
pub fn kill_switch_engaged(ts_ns: u64, source: &str) {
    emit("kill_switch_engaged", ts_ns, [("source", json!(source))]);
}

/// `{"ts_ns","event":"reconcile","ok","detail"?}`. `alerts.py` does not read
/// this event by name today (a discrepancy already raises the generic `halt`
/// line above with `reason:"reconciliation"`); this line exists so a clean
/// reconcile is visible in the log too, per the wave-2 spec's "every ...
/// reconcile result ... writes one JSON line".
pub fn reconcile(ts_ns: u64, ok: bool, detail: Option<&str>) {
    let mut fields: Vec<(&'static str, Value)> = vec![("ok", json!(ok))];
    if let Some(d) = detail {
        fields.push(("detail", json!(d)));
    }
    emit("reconcile", ts_ns, fields);
}

/// `{"ts_ns","event":"accepted_intent",...}`: every order `submit_order_intent`
/// accepts, sim or external mode, so an operator's log has a positive record
/// of what was sent, not just what was rejected or halted.
pub fn accepted_intent(
    ts_ns: u64,
    client_order_id: u64,
    instrument: &str,
    side: &str,
    qty: &str,
    price: &str,
) {
    emit(
        "accepted_intent",
        ts_ns,
        [
            ("client_order_id", json!(client_order_id)),
            ("instrument", json!(instrument)),
            ("side", json!(side)),
            ("qty", json!(qty)),
            ("price", json!(price)),
        ],
    );
}
