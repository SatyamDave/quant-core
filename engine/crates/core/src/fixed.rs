//! Fixed-point decimals for prices and quantities. Money never touches `f64`.

use std::fmt;
use std::str::FromStr;

use serde::Deserialize;

/// Number of decimal places carried by [`Price`] and [`Qty`].
pub const SCALE_DIGITS: u32 = 8;
/// `10^SCALE_DIGITS`: the raw integer that represents 1.0.
pub const SCALE: i64 = 10_i64.pow(SCALE_DIGITS);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseFixedError(String);

impl fmt::Display for ParseFixedError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "invalid fixed-point decimal: {:?}", self.0)
    }
}

impl std::error::Error for ParseFixedError {}

/// Parses a plain decimal such as `-12.345` into a raw value at [`SCALE`].
/// Rejects exponents, more than [`SCALE_DIGITS`] fraction digits, and overflow
/// instead of rounding, so a config typo cannot silently change a limit.
fn parse_raw(s: &str) -> Result<i64, ParseFixedError> {
    let err = || ParseFixedError(s.to_owned());
    let (negative, digits) = match s.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, s),
    };
    let (int_part, frac_part) = digits.split_once('.').unwrap_or((digits, ""));
    let all_digits = |p: &str| p.bytes().all(|b| b.is_ascii_digit());
    if int_part.is_empty()
        || !all_digits(int_part)
        || !all_digits(frac_part)
        || frac_part.len() > SCALE_DIGITS as usize
        || (digits.contains('.') && frac_part.is_empty())
    {
        return Err(err());
    }
    let int: i64 = int_part.parse().map_err(|_| err())?;
    let frac: i64 = if frac_part.is_empty() {
        0
    } else {
        let pad = SCALE_DIGITS - u32::try_from(frac_part.len()).map_err(|_| err())?;
        frac_part.parse::<i64>().map_err(|_| err())? * 10_i64.pow(pad)
    };
    let magnitude = int
        .checked_mul(SCALE)
        .and_then(|v| v.checked_add(frac))
        .ok_or_else(err)?;
    Ok(if negative { -magnitude } else { magnitude })
}

fn fmt_raw(raw: i64, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    let sign = if raw < 0 { "-" } else { "" };
    let abs = raw.unsigned_abs();
    let scale = SCALE.unsigned_abs();
    write!(
        f,
        "{sign}{}.{:0width$}",
        abs / scale,
        abs % scale,
        width = SCALE_DIGITS as usize
    )
}

macro_rules! fixed_type {
    ($(#[$doc:meta])* $name:ident) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default, Deserialize)]
        #[serde(try_from = "String")]
        pub struct $name(i64);

        impl $name {
            pub const ZERO: Self = Self(0);

            /// Wraps a raw value already expressed at [`SCALE`].
            #[must_use]
            pub const fn from_raw(raw: i64) -> Self {
                Self(raw)
            }

            #[must_use]
            pub const fn raw(self) -> i64 {
                self.0
            }

            #[must_use]
            pub fn checked_add(self, rhs: Self) -> Option<Self> {
                self.0.checked_add(rhs.0).map(Self)
            }

            #[must_use]
            pub fn checked_sub(self, rhs: Self) -> Option<Self> {
                self.0.checked_sub(rhs.0).map(Self)
            }
        }

        impl FromStr for $name {
            type Err = ParseFixedError;
            fn from_str(s: &str) -> Result<Self, Self::Err> {
                parse_raw(s).map(Self)
            }
        }

        impl TryFrom<String> for $name {
            type Error = ParseFixedError;
            fn try_from(s: String) -> Result<Self, Self::Error> {
                s.parse()
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                fmt_raw(self.0, f)
            }
        }
    };
}

fixed_type!(
    /// A price, or any quote-currency amount (notional, PnL), at [`SCALE`].
    Price
);
fixed_type!(
    /// A quantity in base units at [`SCALE`]. Negative means short where a
    /// position is meant.
    Qty
);

impl Price {
    /// Notional value `price * qty`, still at [`SCALE`]. `None` on overflow.
    #[must_use]
    pub fn notional(self, qty: Qty) -> Option<Price> {
        let raw = i128::from(self.0) * i128::from(qty.0) / i128::from(SCALE);
        i64::try_from(raw).ok().map(Price)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_formats_and_multiplies_without_floats() {
        let p: Price = "65000.5".parse().unwrap();
        assert_eq!(p.raw(), 6_500_050_000_000);
        assert_eq!(p.to_string(), "65000.50000000");
        assert_eq!("-0.00000001".parse::<Qty>().unwrap().raw(), -1);
        let q: Qty = "0.01".parse().unwrap();
        assert_eq!(p.notional(q).unwrap().to_string(), "650.00500000");
        for bad in [
            "",
            ".5",
            "1.",
            "1e3",
            "0.000000001",
            "--1",
            "1.2.3",
            "99999999999999",
        ] {
            assert!(bad.parse::<Price>().is_err(), "{bad:?} should be rejected");
        }
    }
}
