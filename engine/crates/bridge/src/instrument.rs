//! Instrument config (protocol v1.2, issue #66): symbol, tick/lot rules and a
//! trading-hours gate, loaded from `--instrument <path.toml>`. Absent the
//! flag, [`crate::engine::BridgeEngine`] keeps exactly today's behaviour (the
//! hard-coded instrument `"1"`, no tick/qty-step check, no hours gate) — this
//! module is never constructed in that path.
//!
//! ponytail: `Tz` supports exactly the two zones this build needs (`UTC` and
//! the one sample instrument's `America/New_York`), with the real (not
//! approximated) US daylight-saving rule since it is cheap and a fixed
//! offset would be wrong roughly half the year. A third zone, or a
//! non-US daylight-saving rule, needs a real tz database (`chrono-tz` or
//! `time-tz`), not another hand-rolled arm here.
//!
//! Market calendar (wave3/calendar, closing the gap this file used to flag as
//! a documented limitation): `trading_hours.calendar` optionally names a
//! second TOML file (e.g. `config/instruments/calendars/nyse.toml`), resolved
//! the same cwd-relative way as `limits`, listing full-closure holidays and
//! early-close days. [`TradingHours::is_open`] honours it: a holiday rejects
//! the whole day, an early close shortens `close` for that one date, and a
//! date outside the calendar's own covered years (inferred as the
//! earliest/latest year among its own entries) fails closed rather than
//! assuming a regular session -- the same "never silently fall back" posture
//! as an unparseable instrument config. No `calendar` field keeps exactly
//! today's weekday/time-only gate.

use std::path::PathBuf;
use std::str::FromStr;

use qc_core::{InstrumentId, Price, Qty};
use serde::Deserialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tz {
    Utc,
    AmericaNewYork,
}

impl FromStr for Tz {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "UTC" => Ok(Self::Utc),
            "America/New_York" => Ok(Self::AmericaNewYork),
            other => Err(format!(
                "unsupported trading_hours.tz {other:?}: only \"UTC\" and \"America/New_York\" \
                 are implemented (see this module's doc comment)"
            )),
        }
    }
}

/// Days since the Unix epoch (1970-01-01, a Thursday) to a proleptic
/// Gregorian civil date. Howard Hinnant's `civil_from_days`
/// (public domain, <https://howardhinnant.github.io/date_algorithms.html>);
/// exact for every representable day count, no floating point. All-`i128`
/// internally so no intermediate step can wrap or lose a sign, matching this
/// crate's own money-arithmetic convention (e.g. `daily_pnl` in `engine.rs`).
fn civil_from_days(z: i128) -> (i128, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let y = if m <= 2 { y + 1 } else { y };
    (
        y,
        u32::try_from(m).unwrap_or(1),
        u32::try_from(d).unwrap_or(1),
    )
}

/// The inverse of [`civil_from_days`], needed only to locate a DST boundary
/// (the Nth Sunday of a given month) as a day count.
fn days_from_civil(y: i128, m: u32, d: u32) -> i128 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let mp = i128::from(if m > 2 { m - 3 } else { m + 9 }); // [0, 11]
    let doy = (153 * mp + 2) / 5 + i128::from(d) - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146_097 + doe - 719_468
}

/// 0 = Sunday .. 6 = Saturday (1970-01-01 was a Thursday, weekday 4).
fn weekday_from_days(days: i128) -> u32 {
    u32::try_from((days.rem_euclid(7) + 4).rem_euclid(7)).unwrap_or(0)
}

/// The day-of-month of the `n`th (1-based) occurrence of Sunday in `year`/`month`.
fn nth_sunday(year: i128, month: u32, n: u32) -> u32 {
    let first_of_month = days_from_civil(year, month, 1);
    let first_weekday = weekday_from_days(first_of_month);
    let first_sunday_day = 1 + (7 - first_weekday) % 7;
    first_sunday_day + (n - 1) * 7
}

/// US Eastern DST since 2007: starts the 2nd Sunday of March at 02:00 local
/// standard time (07:00 UTC, `EST = UTC-5`), ends the 1st Sunday of November
/// at 02:00 local daylight time (06:00 UTC, `EDT = UTC-4`). Returns the UTC
/// offset in minutes (`-240` in DST, `-300` otherwise) for the civil year the
/// UTC timestamp's date falls in — correct even for the handful of hours in
/// early January that share a UTC date with the tail of the prior year,
/// because those hours are always outside both boundaries anyway.
fn america_new_york_offset_minutes(epoch_ns: u64) -> i64 {
    let seconds = i128::from(epoch_ns / 1_000_000_000);
    let days = seconds.div_euclid(86_400);
    let (year, _, _) = civil_from_days(days);
    let dst_start_s = days_from_civil(year, 3, nth_sunday(year, 3, 2)) * 86_400 + 7 * 3600;
    let dst_end_s = days_from_civil(year, 11, nth_sunday(year, 11, 1)) * 86_400 + 6 * 3600;
    if seconds >= dst_start_s && seconds < dst_end_s {
        -240
    } else {
        -300
    }
}

impl Tz {
    /// UTC offset in minutes at this instant (east positive, so US zones are negative).
    #[must_use]
    fn offset_minutes(self, epoch_ns: u64) -> i64 {
        match self {
            Self::Utc => 0,
            Self::AmericaNewYork => america_new_york_offset_minutes(epoch_ns),
        }
    }

    /// Local civil date/time at `epoch_ns`: `(days since the Unix epoch in
    /// this zone, weekday 0=Sun..6=Sat, minute_of_day)`. The day count is
    /// what a [`MarketCalendar`] entry is keyed on.
    #[must_use]
    fn local(self, epoch_ns: u64) -> (i128, u32, i64) {
        let seconds = i128::from(epoch_ns / 1_000_000_000);
        let local_s = seconds + i128::from(self.offset_minutes(epoch_ns)) * 60;
        let days = local_s.div_euclid(86_400);
        let minute_of_day = i64::try_from(local_s.rem_euclid(86_400) / 60).unwrap_or(0);
        (days, weekday_from_days(days), minute_of_day)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Weekday {
    Sun,
    Mon,
    Tue,
    Wed,
    Thu,
    Fri,
    Sat,
}

impl Weekday {
    const fn from_index(i: u32) -> Self {
        match i {
            0 => Self::Sun,
            1 => Self::Mon,
            2 => Self::Tue,
            3 => Self::Wed,
            4 => Self::Thu,
            5 => Self::Fri,
            _ => Self::Sat,
        }
    }
}

impl FromStr for Weekday {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "Sun" => Ok(Self::Sun),
            "Mon" => Ok(Self::Mon),
            "Tue" => Ok(Self::Tue),
            "Wed" => Ok(Self::Wed),
            "Thu" => Ok(Self::Thu),
            "Fri" => Ok(Self::Fri),
            "Sat" => Ok(Self::Sat),
            other => Err(format!("unknown weekday {other:?} (use Mon, Tue, ... Sun)")),
        }
    }
}

/// Parses `"HH:MM"` into a minute-of-day (`0..1440`).
fn parse_hhmm(s: &str) -> Result<u32, String> {
    let (h, m) = s
        .split_once(':')
        .ok_or_else(|| format!("{s:?} is not \"HH:MM\""))?;
    let h: u32 = h.parse().map_err(|_| format!("{s:?} is not \"HH:MM\""))?;
    let m: u32 = m.parse().map_err(|_| format!("{s:?} is not \"HH:MM\""))?;
    if h >= 24 || m >= 60 {
        return Err(format!("{s:?} is out of range for \"HH:MM\""));
    }
    Ok(h * 60 + m)
}

/// Parses `"YYYY-MM-DD"` into a day-count since the Unix epoch (via
/// [`days_from_civil`]), the same key [`Tz::local`] returns for `epoch_ns`.
fn parse_date(s: &str) -> Result<i128, String> {
    let bad = || format!("{s:?} is not \"YYYY-MM-DD\"");
    let mut parts = s.splitn(3, '-');
    let (Some(y), Some(m), Some(d)) = (parts.next(), parts.next(), parts.next()) else {
        return Err(bad());
    };
    let y: i128 = y.parse().map_err(|_| bad())?;
    let m: u32 = m.parse().map_err(|_| bad())?;
    let d: u32 = d.parse().map_err(|_| bad())?;
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return Err(format!("{s:?} is out of range for \"YYYY-MM-DD\""));
    }
    Ok(days_from_civil(y, m, d))
}

/// One local calendar day's session, per [`MarketCalendar::session_for`].
enum Session {
    Regular,
    EarlyClose(u32),
    Closed,
}

/// A market holiday/early-close calendar (wave3/calendar), loaded from the
/// file `trading_hours.calendar` names. Coverage is inferred as
/// `[earliest, latest]` year among this file's own entries -- see the module
/// doc comment. Days are Vec-scanned, not a `HashMap` (this crate's
/// `clippy.toml` ban, and there are only a handful of entries per year).
#[derive(Debug, Clone)]
pub struct MarketCalendar {
    min_year: i128,
    max_year: i128,
    holidays: Vec<i128>,
    early_closes: Vec<(i128, u32)>,
}

impl MarketCalendar {
    /// `None` when `days` (the local calendar day `epoch_ns` fell on) is
    /// outside `[min_year, max_year]`: the caller must fail closed, not
    /// assume [`Session::Regular`].
    fn session_for(&self, days: i128) -> Option<Session> {
        let (year, _, _) = civil_from_days(days);
        if year < self.min_year || year > self.max_year {
            return None;
        }
        if self.holidays.contains(&days) {
            return Some(Session::Closed);
        }
        if let Some(&(_, close_minute)) = self.early_closes.iter().find(|(d, _)| *d == days) {
            return Some(Session::EarlyClose(close_minute));
        }
        Some(Session::Regular)
    }
}

#[derive(Debug, Deserialize)]
struct RawHoliday {
    date: String,
    #[allow(dead_code)] // documentation only; the gate reads only `date`
    name: String,
}

#[derive(Debug, Deserialize)]
struct RawEarlyClose {
    date: String,
    close: String,
    #[allow(dead_code)] // documentation only; the gate reads only `date`/`close`
    name: String,
}

#[derive(Debug, Deserialize)]
struct RawMarketCalendar {
    #[serde(default)]
    holidays: Vec<RawHoliday>,
    #[serde(default)]
    early_closes: Vec<RawEarlyClose>,
}

impl TryFrom<RawMarketCalendar> for MarketCalendar {
    type Error = String;
    fn try_from(raw: RawMarketCalendar) -> Result<Self, String> {
        if raw.holidays.is_empty() && raw.early_closes.is_empty() {
            return Err("calendar file has no holidays or early_closes entries".to_owned());
        }
        let holidays = raw
            .holidays
            .iter()
            .map(|h| parse_date(&h.date).map_err(|e| format!("holidays: {e}")))
            .collect::<Result<Vec<i128>, _>>()?;
        let early_closes = raw
            .early_closes
            .iter()
            .map(|e| {
                let days = parse_date(&e.date).map_err(|err| format!("early_closes: {err}"))?;
                let minute = parse_hhmm(&e.close).map_err(|err| format!("early_closes: {err}"))?;
                Ok((days, minute))
            })
            .collect::<Result<Vec<(i128, u32)>, String>>()?;
        let years = holidays
            .iter()
            .chain(early_closes.iter().map(|(d, _)| d))
            .map(|&d| civil_from_days(d).0)
            .collect::<Vec<i128>>();
        // Non-empty: the emptiness check above guarantees at least one entry.
        let min_year = *years.iter().min().expect("checked non-empty above");
        let max_year = *years.iter().max().expect("checked non-empty above");
        Ok(Self {
            min_year,
            max_year,
            holidays,
            early_closes,
        })
    }
}

#[derive(Debug, Clone)]
pub struct TradingHours {
    tz: Tz,
    open_minute: u32,
    close_minute: u32,
    days: Vec<Weekday>,
    calendar: Option<MarketCalendar>,
}

impl TradingHours {
    /// Whether `epoch_ns` (a real UTC nanosecond timestamp; see this
    /// module's doc comment) falls inside this session: `open <= t < close`
    /// on one of `days`, in this zone's local time, then narrowed by the
    /// configured [`MarketCalendar`] (if any) -- a holiday closes the whole
    /// day, an early close replaces `close` for that date, and a date
    /// outside the calendar's own covered years fails closed rather than
    /// assuming a regular session.
    #[must_use]
    pub fn is_open(&self, epoch_ns: u64) -> bool {
        let (days, weekday, minute_of_day) = self.tz.local(epoch_ns);
        if !self.days.contains(&Weekday::from_index(weekday))
            || minute_of_day < i64::from(self.open_minute)
        {
            return false;
        }
        let close_minute = match &self.calendar {
            None => self.close_minute,
            Some(calendar) => match calendar.session_for(days) {
                None | Some(Session::Closed) => return false,
                Some(Session::Regular) => self.close_minute,
                Some(Session::EarlyClose(minute)) => minute,
            },
        };
        minute_of_day < i64::from(close_minute)
    }
}

#[derive(Debug, Deserialize)]
struct RawTradingHours {
    tz: String,
    open: String,
    close: String,
    days: Vec<String>,
    /// Path to an optional [`MarketCalendar`] file, resolved relative to the
    /// instrument config file's own directory (same convention as `limits`).
    #[serde(default)]
    calendar: Option<String>,
}

impl TradingHours {
    /// # Errors
    /// A field that does not parse, an empty `days` list, or (when
    /// `calendar` is set) a calendar file that is missing, unreadable, or
    /// itself fails to parse. Fails closed, matching [`Instrument::parse`]'s
    /// own contract: never silently falls back to no calendar.
    fn from_raw(raw: &RawTradingHours, config_dir: &std::path::Path) -> Result<Self, String> {
        let days = raw
            .days
            .iter()
            .map(|d| d.parse())
            .collect::<Result<Vec<Weekday>, _>>()?;
        if days.is_empty() {
            return Err("trading_hours.days must not be empty".to_owned());
        }
        let calendar = raw
            .calendar
            .as_ref()
            .map(|rel| {
                let path = config_dir.join(rel);
                let text = std::fs::read_to_string(&path)
                    .map_err(|e| format!("trading_hours.calendar {}: {e}", path.display()))?;
                let raw_calendar: RawMarketCalendar = toml::from_str(&text)
                    .map_err(|e| format!("trading_hours.calendar {}: {e}", path.display()))?;
                MarketCalendar::try_from(raw_calendar)
                    .map_err(|e| format!("trading_hours.calendar {}: {e}", path.display()))
            })
            .transpose()?;
        Ok(Self {
            tz: raw.tz.parse()?,
            open_minute: parse_hhmm(&raw.open)?,
            close_minute: parse_hhmm(&raw.close)?,
            days,
            calendar,
        })
    }
}

#[derive(Debug, Deserialize)]
struct RawInstrument {
    id: u32,
    symbol: String,
    tick_size: String,
    qty_step: String,
    min_qty: String,
    allows_fractional: bool,
    trading_hours: RawTradingHours,
    limits: String,
}

#[derive(Debug, Clone)]
pub struct Instrument {
    pub id: InstrumentId,
    pub symbol: String,
    pub tick_size: Price,
    pub qty_step: Qty,
    pub min_qty: Qty,
    #[allow(dead_code)] // carried for the TS gateway / a future order-entry UI; not yet read here
    pub allows_fractional: bool,
    pub trading_hours: TradingHours,
    /// Path to the per-instrument limits file, resolved relative to the
    /// instrument config file's own directory (matches `--limits`'s own
    /// cwd-relative convention in `main.rs`).
    pub limits_path: PathBuf,
}

impl Instrument {
    /// # Errors
    /// A field that does not parse (bad decimal, unknown tz/weekday, `"HH:MM"`
    /// out of range) or an empty `days` list. Fails closed: an instrument
    /// config the bridge cannot fully understand never silently falls back
    /// to the hard-coded default.
    pub fn parse(toml_text: &str, config_dir: &std::path::Path) -> Result<Self, String> {
        let raw: RawInstrument = toml::from_str(toml_text).map_err(|e| e.to_string())?;
        Ok(Self {
            id: InstrumentId(raw.id),
            symbol: raw.symbol,
            tick_size: raw
                .tick_size
                .parse()
                .map_err(|e| format!("tick_size: {e}"))?,
            qty_step: raw.qty_step.parse().map_err(|e| format!("qty_step: {e}"))?,
            min_qty: raw.min_qty.parse().map_err(|e| format!("min_qty: {e}"))?,
            allows_fractional: raw.allows_fractional,
            trading_hours: TradingHours::from_raw(&raw.trading_hours, config_dir)?,
            limits_path: config_dir.join(raw.limits),
        })
    }

    /// `qty` must be a positive multiple of `qty_step` and at least `min_qty`.
    #[must_use]
    pub fn qty_is_valid(&self, qty: Qty) -> bool {
        self.qty_step.raw() > 0
            && qty.raw() > 0
            && qty.raw() % self.qty_step.raw() == 0
            && qty >= self.min_qty
    }

    /// `price` must be a positive multiple of `tick_size`.
    #[must_use]
    pub fn price_is_valid(&self, price: Price) -> bool {
        self.tick_size.raw() > 0 && price.raw() > 0 && price.raw() % self.tick_size.raw() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ns(y: i128, m: u32, d: u32, h: u32, min: u32) -> u64 {
        let days = days_from_civil(y, m, d);
        let secs = days * 86_400 + i128::from(h) * 3600 + i128::from(min) * 60;
        u64::try_from(secs).unwrap() * 1_000_000_000
    }

    #[test]
    fn civil_from_days_round_trips_through_days_from_civil() {
        for days in [-100_000_i128, -1, 0, 1, 19_000, 19_797, 30_000] {
            let (y, m, d) = civil_from_days(days);
            assert_eq!(days_from_civil(y, m, d), days, "{y}-{m}-{d}");
        }
    }

    #[test]
    fn epoch_zero_is_1970_01_01_a_thursday() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(weekday_from_days(0), 4, "1970-01-01 was a Thursday");
    }

    #[test]
    fn us_eastern_is_standard_time_in_january() {
        // 2026-01-15 12:00 UTC must be EST (UTC-5): 07:00 local.
        let t = ns(2026, 1, 15, 12, 0);
        assert_eq!(Tz::AmericaNewYork.offset_minutes(t), -300);
        let (days, weekday, minute) = Tz::AmericaNewYork.local(t);
        assert_eq!((weekday, minute), (4, 7 * 60)); // Thursday, 07:00
        assert_eq!(civil_from_days(days), (2026, 1, 15));
    }

    #[test]
    fn us_eastern_is_daylight_time_in_july() {
        // 2026-07-15 12:00 UTC must be EDT (UTC-4): 08:00 local.
        let t = ns(2026, 7, 15, 12, 0);
        assert_eq!(Tz::AmericaNewYork.offset_minutes(t), -240);
        let (days, weekday, minute) = Tz::AmericaNewYork.local(t);
        assert_eq!((weekday, minute), (3, 8 * 60)); // Wednesday, 08:00
        assert_eq!(civil_from_days(days), (2026, 7, 15));
    }

    #[test]
    fn dst_boundaries_for_2026_match_the_published_us_rule() {
        // 2026: DST starts Sunday 2026-03-08, ends Sunday 2026-11-01 (2nd
        // Sunday of March, 1st Sunday of November).
        let just_before_start = ns(2026, 3, 8, 6, 59);
        let just_after_start = ns(2026, 3, 8, 7, 0);
        assert_eq!(Tz::AmericaNewYork.offset_minutes(just_before_start), -300);
        assert_eq!(Tz::AmericaNewYork.offset_minutes(just_after_start), -240);

        let just_before_end = ns(2026, 11, 1, 5, 59);
        let just_after_end = ns(2026, 11, 1, 6, 0);
        assert_eq!(Tz::AmericaNewYork.offset_minutes(just_before_end), -240);
        assert_eq!(Tz::AmericaNewYork.offset_minutes(just_after_end), -300);
    }

    fn spy_hours() -> TradingHours {
        spy_hours_raw(None)
    }

    fn spy_hours_raw(calendar: Option<String>) -> TradingHours {
        TradingHours::from_raw(
            &RawTradingHours {
                tz: "America/New_York".to_owned(),
                open: "09:30".to_owned(),
                close: "16:00".to_owned(),
                days: ["Mon", "Tue", "Wed", "Thu", "Fri"]
                    .map(str::to_owned)
                    .to_vec(),
                calendar,
            },
            std::path::Path::new("."),
        )
        .unwrap()
    }

    #[test]
    fn regular_session_is_open_at_10am_eastern_on_a_weekday() {
        // 2026-07-15 is a Wednesday; 10:00 EDT = 14:00 UTC.
        assert!(spy_hours().is_open(ns(2026, 7, 15, 14, 0)));
    }

    #[test]
    fn regular_session_is_closed_before_open_and_at_and_after_close() {
        // 09:29 EDT = 13:29 UTC (closed), 16:00 EDT = 20:00 UTC (closed: half-open interval).
        assert!(!spy_hours().is_open(ns(2026, 7, 15, 13, 29)));
        assert!(!spy_hours().is_open(ns(2026, 7, 15, 20, 0)));
        assert!(spy_hours().is_open(ns(2026, 7, 15, 19, 59)));
    }

    #[test]
    fn regular_session_is_closed_on_a_weekend() {
        // 2026-07-18 is a Saturday; 10:00 EDT = 14:00 UTC.
        assert!(!spy_hours().is_open(ns(2026, 7, 18, 14, 0)));
    }

    #[test]
    fn unsupported_timezone_fails_closed() {
        assert!("Europe/London".parse::<Tz>().is_err());
    }

    /// [`spy_hours`] with an in-memory [`MarketCalendar`] spliced in directly
    /// (no temp file / disk round trip needed to test [`TradingHours::is_open`]'s
    /// calendar wiring).
    fn spy_hours_calendar(holidays: &[&str], early_closes: &[(&str, &str)]) -> TradingHours {
        TradingHours {
            calendar: Some(calendar(holidays, early_closes)),
            ..spy_hours()
        }
    }

    fn calendar(holidays: &[&str], early_closes: &[(&str, &str)]) -> MarketCalendar {
        RawMarketCalendar {
            holidays: holidays
                .iter()
                .map(|d| RawHoliday {
                    date: (*d).to_owned(),
                    name: "test".to_owned(),
                })
                .collect(),
            early_closes: early_closes
                .iter()
                .map(|(d, c)| RawEarlyClose {
                    date: (*d).to_owned(),
                    close: (*c).to_owned(),
                    name: "test".to_owned(),
                })
                .collect(),
        }
        .try_into()
        .unwrap()
    }

    #[test]
    fn calendar_covered_years_are_inferred_from_its_own_entries() {
        // Entries only in 2026 and 2027: coverage is exactly [2026, 2027].
        let cal = calendar(&["2026-01-01", "2027-12-24"], &[]);
        assert!(matches!(
            cal.session_for(days_from_civil(2026, 6, 1)),
            Some(Session::Regular)
        ));
        assert!(matches!(
            cal.session_for(days_from_civil(2027, 6, 1)),
            Some(Session::Regular)
        ));
        assert!(
            cal.session_for(days_from_civil(2025, 12, 31)).is_none(),
            "before the earliest covered year must fail closed (None), not Regular"
        );
        assert!(
            cal.session_for(days_from_civil(2028, 1, 1)).is_none(),
            "after the latest covered year must fail closed (None), not Regular"
        );
    }

    #[test]
    fn calendar_marks_a_holiday_closed_and_an_early_close_shortened() {
        let cal = calendar(&["2026-11-26"], &[("2026-11-27", "13:00")]);
        assert!(matches!(
            cal.session_for(days_from_civil(2026, 11, 26)),
            Some(Session::Closed)
        ));
        assert!(matches!(
            cal.session_for(days_from_civil(2026, 11, 27)),
            Some(Session::EarlyClose(m)) if m == 13 * 60
        ));
    }

    #[test]
    fn calendar_with_no_entries_fails_to_parse() {
        let raw = RawMarketCalendar {
            holidays: vec![],
            early_closes: vec![],
        };
        assert!(MarketCalendar::try_from(raw).is_err());
    }

    #[test]
    fn calendar_rejects_an_unparseable_date() {
        let raw = RawMarketCalendar {
            holidays: vec![RawHoliday {
                date: "11/26/2026".to_owned(),
                name: "bad format".to_owned(),
            }],
            early_closes: vec![],
        };
        assert!(MarketCalendar::try_from(raw).is_err());
    }

    #[test]
    fn instrument_with_a_holiday_calendar_is_closed_all_day_on_the_holiday() {
        // Same as spy_hours() but with a calendar closing Thanksgiving 2026
        // and shortening the day after it to 13:00 ET.
        let hours = spy_hours_calendar(&["2026-11-26"], &[("2026-11-27", "13:00")]);
        // 2026-11-26 is a Thursday, a normal trading weekday absent the calendar.
        assert!(
            !hours.is_open(ns(2026, 11, 26, 15, 0)),
            "10:00 EST, but a holiday"
        );
        // 2026-11-27 (Friday): open before 13:00 ET, closed at/after it.
        assert!(hours.is_open(ns(2026, 11, 27, 17, 59)), "12:59 EST");
        assert!(
            !hours.is_open(ns(2026, 11, 27, 18, 0)),
            "13:00 EST: early close"
        );
        // A normal weekday elsewhere in the covered range is unaffected.
        assert!(hours.is_open(ns(2026, 7, 15, 14, 0)));
    }

    #[test]
    fn instrument_with_a_calendar_fails_closed_outside_its_covered_years() {
        let hours = spy_hours_calendar(&["2026-11-26"], &[]);
        // 2028-07-12 is a Wednesday, 10:00 EDT: a normal trading day/time by
        // weekday and hours alone, but 2028 has no calendar coverage.
        assert!(!hours.is_open(ns(2028, 7, 12, 14, 0)));
    }

    #[test]
    fn qty_and_price_step_checks() {
        let inst = Instrument {
            id: InstrumentId(2),
            symbol: "SPY".to_owned(),
            tick_size: "0.01".parse().unwrap(),
            qty_step: "1".parse().unwrap(),
            min_qty: "1".parse().unwrap(),
            allows_fractional: false,
            trading_hours: spy_hours(),
            limits_path: PathBuf::from("spy.toml"),
        };
        assert!(inst.qty_is_valid("1".parse().unwrap()));
        assert!(inst.qty_is_valid("3".parse().unwrap()));
        assert!(!inst.qty_is_valid("0.5".parse().unwrap()));
        assert!(!inst.qty_is_valid("0".parse().unwrap()));
        assert!(inst.price_is_valid("450.12".parse().unwrap()));
        assert!(!inst.price_is_valid("450.125".parse().unwrap()));
    }
}
