//! Feature set `tob-v1`, the Rust side of the single definition in
//! `ml/features/__init__.py`. Both must change together, with a new
//! [`FEATURE_VERSION`] and a regenerated parity fixture.
//!
//! Integer arithmetic on raw fixed-point values is exact; floats appear only in
//! the final ratios, in the same operation order as the Python code.

use qc_core::{Price, Qty};

pub const FEATURE_VERSION: &str = "tob-v1";
pub const FEATURE_NAMES: [&str; N_FEATURES] = [
    "ofi_norm",
    "queue_imbalance",
    "microprice_dev_ticks",
    "spread_ticks",
];
pub const N_FEATURES: usize = 4;
pub const OFI_WINDOW: usize = 10;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TopOfBook {
    pub bid_px: Price,
    pub bid_qty: Qty,
    pub ask_px: Price,
    pub ask_qty: Qty,
}

impl TopOfBook {
    fn valid(&self) -> bool {
        self.bid_qty.raw() > 0 && self.ask_qty.raw() > 0 && self.bid_px < self.ask_px
    }
}

/// One event's order-flow imbalance in raw qty units (Cont, Kukanov & Stoikov 2014, eq. 2).
fn ofi_contribution(prev: &TopOfBook, cur: &TopOfBook) -> i128 {
    let mut e = 0_i128;
    if cur.bid_px >= prev.bid_px {
        e += i128::from(cur.bid_qty.raw());
    }
    if cur.bid_px <= prev.bid_px {
        e -= i128::from(prev.bid_qty.raw());
    }
    if cur.ask_px <= prev.ask_px {
        e -= i128::from(cur.ask_qty.raw());
    }
    if cur.ask_px >= prev.ask_px {
        e += i128::from(prev.ask_qty.raw());
    }
    e
}

/// Streaming feature state; fixed size, no allocation per update.
#[derive(Debug, Clone)]
pub struct FeatureState {
    tick: Price,
    prev: Option<TopOfBook>,
    ofi: [i128; OFI_WINDOW],
    len: usize,
    next: usize,
}

impl FeatureState {
    #[must_use]
    pub fn new(tick: Price) -> Self {
        Self {
            tick,
            prev: None,
            ofi: [0; OFI_WINDOW],
            len: 0,
            next: 0,
        }
    }

    /// Features for this book, or `None` while warming up or on an invalid
    /// (one-sided, empty, or crossed) book, which also resets the window.
    // Features are unitless ratios; converting raw integers to f64 here is the
    // one intended place floats enter, and money never does.
    #[allow(clippy::cast_precision_loss)]
    pub fn update(&mut self, tob: TopOfBook) -> Option<[f64; N_FEATURES]> {
        if !tob.valid() || self.tick.raw() <= 0 {
            self.prev = None;
            self.len = 0;
            self.next = 0;
            return None;
        }
        if let Some(prev) = self.prev {
            self.ofi[self.next] = ofi_contribution(&prev, &tob);
            self.next = (self.next + 1) % OFI_WINDOW;
            self.len = (self.len + 1).min(OFI_WINDOW);
        }
        self.prev = Some(tob);
        if self.len < OFI_WINDOW {
            return None;
        }
        let bid_qty = i128::from(tob.bid_qty.raw());
        let ask_qty = i128::from(tob.ask_qty.raw());
        let depth = (bid_qty + ask_qty) as f64;
        let ofi_norm = self.ofi.iter().sum::<i128>() as f64 / depth;
        let qi = (bid_qty - ask_qty) as f64 / depth;
        let spread = i128::from(tob.ask_px.raw()) - i128::from(tob.bid_px.raw());
        let spread_ticks = spread as f64 / self.tick.raw() as f64;
        let microprice_dev = 0.5 * spread_ticks * qi;
        Some([ofi_norm, qi, microprice_dev, spread_ticks])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tob(bid: i64, bq: i64, ask: i64, aq: i64) -> TopOfBook {
        TopOfBook {
            bid_px: Price::from_raw(bid),
            bid_qty: Qty::from_raw(bq),
            ask_px: Price::from_raw(ask),
            ask_qty: Qty::from_raw(aq),
        }
    }

    #[test]
    #[allow(clippy::float_cmp)] // small exact ratios, so equality is exact
    fn warms_up_then_resets_on_crossed_book() {
        let mut s = FeatureState::new(Price::from_raw(1));
        for _ in 0..OFI_WINDOW {
            assert_eq!(s.update(tob(10, 3, 12, 1)), None);
        }
        let f = s.update(tob(10, 3, 12, 1)).expect("window full");
        // Unchanged book: each event adds +bid_qty -bid_qty -ask_qty +ask_qty = 0.
        assert_eq!(f, [0.0, 0.5, 0.5, 2.0]);
        assert_eq!(s.update(tob(12, 3, 12, 1)), None);
        assert_eq!(s.update(tob(10, 3, 12, 1)), None);
    }
}
