//! Protocol v1.2, issue #66: `--instrument` teaches the engine a real ticker,
//! tick size, whole-share lots and trading hours, in place of the v1
//! hard-coded instrument `"1"`. Timestamps here are real UTC epoch
//! nanoseconds (unlike most fixtures' small synthetic counters), because
//! trading-hours gating reads a real calendar date out of them.
//!
//! Wave3/calendar: `config/instruments/spy.toml` now references
//! `config/instruments/calendars/nyse.toml` (NYSE holidays and early closes
//! for 2026-2027; source + access date in that file's header), and
//! `support::spy_instrument()` parses the real committed files, so the tests
//! below exercise the actual shipped calendar, not a hand-typed copy of it.

mod support;

use qc_bridge::handle_line;
use qc_core::{OrderType, Side};
use serde_json::Value;

// 2026-07-15 is a Wednesday; DST is in effect (EDT, UTC-4).
const IN_HOURS_NS: u64 = 1_784_126_100_000_000_000; // 14:35 UTC = 10:35 EDT
const AFTER_CLOSE_NS: u64 = 1_784_149_200_000_000_000; // 21:00 UTC = 17:00 EDT, after the 16:00 close

// -- Date/time helpers, duplicated from `qc_bridge::instrument` (private
// there, and already proved correct by that module's own DST unit tests:
// `dst_boundaries_for_2026_match_the_published_us_rule` etc.) so this
// integration test can build real epoch-nanosecond timestamps for arbitrary
// America/New_York local dates/times without a date-library dependency.

fn days_from_civil(y: i128, m: u32, d: u32) -> i128 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let mp = i128::from(if m > 2 { m - 3 } else { m + 9 });
    let doy = (153 * mp + 2) / 5 + i128::from(d) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn weekday_from_days(days: i128) -> u32 {
    u32::try_from((days.rem_euclid(7) + 4).rem_euclid(7)).unwrap_or(0)
}

fn nth_sunday(year: i128, month: u32, n: u32) -> u32 {
    let first_of_month = days_from_civil(year, month, 1);
    let first_weekday = weekday_from_days(first_of_month);
    let first_sunday_day = 1 + (7 - first_weekday) % 7;
    first_sunday_day + (n - 1) * 7
}

fn is_dst_for_year(year: i128, utc_seconds: i128) -> bool {
    let dst_start_s = days_from_civil(year, 3, nth_sunday(year, 3, 2)) * 86_400 + 7 * 3600;
    let dst_end_s = days_from_civil(year, 11, nth_sunday(year, 11, 1)) * 86_400 + 6 * 3600;
    utc_seconds >= dst_start_s && utc_seconds < dst_end_s
}

/// Epoch nanoseconds for `h:min` `America/New_York` local time on `y-m-d`.
/// Every date this file uses is well clear of the DST transition instant
/// (2am local on a specific Sunday), so guessing standard time to look up
/// the real offset never flips the answer.
fn eastern_ns(y: i128, m: u32, d: u32, h: u32, min: u32) -> u64 {
    let days = days_from_civil(y, m, d);
    let local_s = days * 86_400 + i128::from(h) * 3600 + i128::from(min) * 60;
    let guess_utc_s = local_s + 300 * 60; // assume EST just to pick a year/side for the DST check
    let offset_min: i64 = if is_dst_for_year(y, guess_utc_s) {
        -240
    } else {
        -300
    };
    let utc_s = local_s - i128::from(offset_min) * 60;
    u64::try_from(utc_s).unwrap() * 1_000_000_000
}

/// Every NYSE full-market-closure date for 2026-2027, per
/// `config/instruments/calendars/nyse.toml`: (name, year, month, day).
const NYSE_HOLIDAYS_2026_2027: &[(&str, i128, u32, u32)] = &[
    ("New Year's Day 2026", 2026, 1, 1),
    ("MLK Day 2026", 2026, 1, 19),
    ("Washington's Birthday 2026", 2026, 2, 16),
    ("Good Friday 2026", 2026, 4, 3),
    ("Memorial Day 2026", 2026, 5, 25),
    ("Juneteenth 2026", 2026, 6, 19),
    ("Independence Day 2026 (observed)", 2026, 7, 3),
    ("Labor Day 2026", 2026, 9, 7),
    ("Thanksgiving 2026", 2026, 11, 26),
    ("Christmas 2026", 2026, 12, 25),
    ("New Year's Day 2027", 2027, 1, 1),
    ("MLK Day 2027", 2027, 1, 18),
    ("Washington's Birthday 2027", 2027, 2, 15),
    ("Good Friday 2027", 2027, 3, 26),
    ("Memorial Day 2027", 2027, 5, 31),
    ("Juneteenth 2027 (observed)", 2027, 6, 18),
    ("Independence Day 2027 (observed)", 2027, 7, 5),
    ("Labor Day 2027", 2027, 9, 6),
    ("Thanksgiving 2027", 2027, 11, 25),
    ("Christmas 2027 (observed)", 2027, 12, 24),
];

/// Every NYSE 1:00 p.m. ET early-close date for 2026-2027, same source.
const NYSE_EARLY_CLOSES_2026_2027: &[(&str, i128, u32, u32)] = &[
    ("Day after Thanksgiving 2026", 2026, 11, 27),
    ("Christmas Eve 2026", 2026, 12, 24),
    ("Day after Thanksgiving 2027", 2027, 11, 26),
];

/// Priced near $20 so one whole share fits `config/limits/spy.toml`'s $25
/// per-order cap; a real $100+ share is
/// rejected, see `a_one_share_order_above_the_per_order_cap_is_rejected`.
fn spy_csv() -> String {
    format!(
        "S,2,10,{IN_HOURS_NS},{IN_HOURS_NS},20.00@1;19.99@1,20.02@1;20.03@1\n\
         D,2,11,B,19.98,1,{AFTER_CLOSE_NS},{AFTER_CLOSE_NS}\n"
    )
}

fn call(engine: &mut qc_bridge::BridgeEngine, line: &str) -> Value {
    let (response, _) = handle_line(engine, line);
    serde_json::from_str(&response).unwrap()
}

#[test]
fn decision_request_carries_the_configured_symbol() {
    let mut engine = support::engine_with_instrument(&spy_csv(), 1);
    let dr = engine.next_decision_request().expect("book is synced");
    assert_eq!(dr["instrument"], "SPY");
}

#[test]
fn allowed_actions_excludes_buy_sell_outside_trading_hours() {
    let mut engine = support::engine_with_instrument(&spy_csv(), 1);
    let first = engine.next_decision_request().expect("in hours");
    assert_eq!(
        first["allowed_actions"],
        serde_json::json!(["buy", "sell", "no_trade"])
    );
    let second = engine.next_decision_request().expect("after close");
    assert_eq!(
        second["allowed_actions"],
        serde_json::json!(["no_trade"]),
        "the engine, not the agent, decides what is offerable outside hours"
    );
}

#[test]
fn submit_is_accepted_in_hours_and_rejected_outside_hours() {
    let mut engine = support::engine_with_instrument(&spy_csv(), 1);
    assert!(engine.next_decision_request().is_some(), "in hours");

    let ok = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "1".parse().unwrap(),
        "20.00".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(ok["accepted"], true, "{ok:#}");

    assert!(
        engine.next_decision_request().is_some(),
        "advances past close"
    );
    let rejected = engine.submit_order_intent(
        "dr-2",
        Side::Buy,
        "1".parse().unwrap(),
        "20.00".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(rejected["accepted"], false);
    assert_eq!(rejected["risk_reject"], "outside_trading_hours");
}

#[test]
fn submit_is_rejected_for_a_qty_that_is_not_a_whole_share() {
    let mut engine = support::engine_with_instrument(&spy_csv(), 1);
    assert!(engine.next_decision_request().is_some());
    let resp = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "0.5".parse().unwrap(),
        "20.00".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(resp["accepted"], false);
    assert_eq!(resp["risk_reject"], "invalid_qty_step");
}

#[test]
fn submit_is_rejected_for_a_price_finer_than_a_cent() {
    let mut engine = support::engine_with_instrument(&spy_csv(), 1);
    assert!(engine.next_decision_request().is_some());
    let resp = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "1".parse().unwrap(),
        "20.001".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(resp["accepted"], false);
    assert_eq!(resp["risk_reject"], "invalid_tick_size");
}

#[test]
fn wire_level_intent_must_match_the_configured_symbol() {
    let mut engine = support::engine_with_instrument(&spy_csv(), 1);
    assert!(engine.next_decision_request().is_some());
    let wrong_symbol = r#"{"v":1,"id":"1","op":"submit_order_intent","intent":
        {"request_id":"dr-1","instrument":"1","side":"buy","qty":"1",
         "limit_price":"20.00","time_in_force":"gtc","reason":"test"}}"#;
    let resp = call(&mut engine, wrong_symbol);
    assert_eq!(resp["ok"], false);
    assert_eq!(resp["error"]["code"], "invalid_fields");

    let right_symbol = r#"{"v":1,"id":"2","op":"submit_order_intent","intent":
        {"request_id":"dr-1","instrument":"SPY","side":"buy","qty":"1",
         "limit_price":"20.00","time_in_force":"gtc","reason":"test"}}"#;
    let resp = call(&mut engine, right_symbol);
    assert_eq!(resp["ok"], true);
    assert_eq!(resp["result"]["accepted"], true, "{resp:#}");
}

#[test]
fn a_one_share_order_above_the_per_order_cap_is_rejected() {
    let csv = format!("S,2,10,{IN_HOURS_NS},{IN_HOURS_NS},100.00@1;99.99@1,100.02@1;100.03@1\n");
    let mut engine = support::engine_with_instrument(&csv, 1);
    assert!(engine.next_decision_request().is_some());
    let resp = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "1".parse().unwrap(),
        "100.00".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(resp["accepted"], false);
    assert_eq!(resp["risk_reject"], "max_notional");
}

// -- Wave3/calendar: NYSE holidays and early closes (config/instruments/calendars/nyse.toml). --

#[test]
fn every_2026_and_2027_nyse_holiday_is_closed_all_day() {
    let instrument = support::spy_instrument();
    for &(name, y, m, d) in NYSE_HOLIDAYS_2026_2027 {
        // Mid-morning and mid-afternoon: well inside the regular 09:30-16:00
        // session by weekday/time alone, so a rejection can only come from
        // the calendar.
        assert!(
            !instrument.trading_hours.is_open(eastern_ns(y, m, d, 10, 0)),
            "{name} ({y}-{m:02}-{d:02}) 10:00 ET must be closed (NYSE holiday)"
        );
        assert!(
            !instrument.trading_hours.is_open(eastern_ns(y, m, d, 14, 0)),
            "{name} ({y}-{m:02}-{d:02}) 14:00 ET must be closed (NYSE holiday)"
        );
    }
}

#[test]
fn every_2026_and_2027_early_close_opens_until_1pm_et_and_closes_at_it() {
    let instrument = support::spy_instrument();
    for &(name, y, m, d) in NYSE_EARLY_CLOSES_2026_2027 {
        assert!(
            instrument.trading_hours.is_open(eastern_ns(y, m, d, 9, 35)),
            "{name} ({y}-{m:02}-{d:02}) 09:35 ET must still be open"
        );
        assert!(
            instrument
                .trading_hours
                .is_open(eastern_ns(y, m, d, 12, 59)),
            "{name} ({y}-{m:02}-{d:02}) 12:59 ET must still be open (early close is 13:00)"
        );
        assert!(
            !instrument.trading_hours.is_open(eastern_ns(y, m, d, 13, 0)),
            "{name} ({y}-{m:02}-{d:02}) 13:00 ET must be closed (early close)"
        );
        assert!(
            !instrument.trading_hours.is_open(eastern_ns(y, m, d, 15, 0)),
            "{name} ({y}-{m:02}-{d:02}) 15:00 ET must be closed (early close)"
        );
    }
}

#[test]
fn a_date_before_the_calendars_earliest_covered_year_fails_closed() {
    // 2025-07-16 is a Wednesday, 10:00 ET: a normal trading weekday/time by
    // `trading_hours` alone, but 2025 has no entry in nyse.toml at all.
    let instrument = support::spy_instrument();
    assert!(
        !instrument
            .trading_hours
            .is_open(eastern_ns(2025, 7, 16, 10, 0))
    );
}

#[test]
fn a_date_after_the_calendars_latest_covered_year_fails_closed() {
    // 2028-07-12 is a Wednesday, 10:00 ET: same shape, but after 2027.
    let instrument = support::spy_instrument();
    assert!(
        !instrument
            .trading_hours
            .is_open(eastern_ns(2028, 7, 12, 10, 0))
    );
}

#[test]
fn thanksgiving_2026_rejects_through_the_full_engine_as_outside_trading_hours() {
    // Thanksgiving 2026-11-26 is a Thursday -- a normal trading weekday by
    // `trading_hours.days` alone -- so a rejection here can only come from
    // the calendar, exercised through the real `submit_order_intent` path
    // (not just `TradingHours::is_open` directly).
    let thanksgiving_ns = eastern_ns(2026, 11, 26, 10, 0);
    let csv =
        format!("S,2,10,{thanksgiving_ns},{thanksgiving_ns},100.00@1;99.99@1,100.02@1;100.03@1\n");
    let mut engine = support::engine_with_instrument(&csv, 1);
    let dr = engine.next_decision_request().expect("book is synced");
    assert_eq!(
        dr["allowed_actions"],
        serde_json::json!(["no_trade"]),
        "a holiday is not offerable, same as any other outside-hours instant"
    );

    let resp = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "1".parse().unwrap(),
        "100.00".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(resp["accepted"], false);
    assert_eq!(resp["risk_reject"], "outside_trading_hours");
}
