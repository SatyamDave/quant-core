//! L2 order book for one instrument, built from a snapshot plus [`BookDelta`]s.
//!
//! Invariants after every call, whether it succeeded or not:
//! - each side is strictly sorted best-first with no zero-quantity level;
//! - the book is never crossed or locked (best bid < best ask);
//! - each side holds at most [`MAX_LEVELS`] levels.
//!
//! A delta that would cross the book, or that arrives out of sequence, is
//! rejected without changing the levels, and the book moves to
//! [`BookStatus::NeedsResync`]. From then on every delta is refused until
//! [`OrderBook::apply_snapshot`] succeeds. A new book also needs a snapshot.

use qc_core::{BookDelta, InstrumentId, Price, Qty, Side, Timestamp};

/// Levels kept per side. Levels worse than the deepest kept one are dropped,
/// which matches depth-limited venue feeds. Capacity is reserved up front so
/// applying a delta never allocates.
pub const MAX_LEVELS: usize = 256;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BookError {
    WrongInstrument {
        expected: InstrumentId,
        got: InstrumentId,
    },
    /// A sequence number was skipped or repeated; the book must resync from a snapshot.
    SequenceGap { expected: u64, got: u64 },
    /// Applying the delta would leave best bid >= best ask; the book must resync.
    WouldCross { side: Side, price: Price },
    /// A delta carried a negative quantity; the book must resync.
    NegativeQty,
    /// The book is waiting for a snapshot and refuses deltas until then.
    NeedsResync,
    /// A snapshot was unsorted, crossed, had a non-positive quantity, or was too deep.
    InvalidSnapshot,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BookStatus {
    Synced,
    NeedsResync,
}

/// A full picture of the book at `seq`, best level first on each side.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BookSnapshot {
    pub instrument: InstrumentId,
    pub seq: u64,
    pub bids: Vec<(Price, Qty)>,
    pub asks: Vec<(Price, Qty)>,
    pub ts_exchange: Timestamp,
    pub ts_local: Timestamp,
}

#[derive(Debug, Clone)]
pub struct OrderBook {
    instrument: InstrumentId,
    /// Sorted best-first: descending for bids, ascending for asks.
    bids: Vec<(Price, Qty)>,
    asks: Vec<(Price, Qty)>,
    status: BookStatus,
    last_seq: u64,
    last_update: Timestamp,
}

/// True when `a` is a strictly better price than `b` on `side`.
#[must_use]
pub fn better(side: Side, a: Price, b: Price) -> bool {
    match side {
        Side::Buy => a > b,
        Side::Sell => a < b,
    }
}

/// Index of `price` in a best-first side, or where it would be inserted.
fn search(levels: &[(Price, Qty)], side: Side, price: Price) -> Result<usize, usize> {
    levels.binary_search_by(|(p, _)| match side {
        Side::Buy => price.cmp(p),
        Side::Sell => p.cmp(&price),
    })
}

fn side_is_valid(levels: &[(Price, Qty)], side: Side) -> bool {
    levels.len() <= MAX_LEVELS
        && levels.iter().all(|(_, q)| *q > Qty::ZERO)
        && levels.windows(2).all(|w| better(side, w[0].0, w[1].0))
}

impl OrderBook {
    #[must_use]
    pub fn new(instrument: InstrumentId) -> Self {
        Self {
            instrument,
            bids: Vec::with_capacity(MAX_LEVELS + 1),
            asks: Vec::with_capacity(MAX_LEVELS + 1),
            status: BookStatus::NeedsResync,
            last_seq: 0,
            last_update: Timestamp(0),
        }
    }

    /// Replaces the whole book. On error the book is left empty and still needs a resync.
    ///
    /// # Errors
    /// [`BookError::WrongInstrument`] or [`BookError::InvalidSnapshot`].
    pub fn apply_snapshot(&mut self, snapshot: &BookSnapshot) -> Result<(), BookError> {
        if snapshot.instrument != self.instrument {
            return Err(BookError::WrongInstrument {
                expected: self.instrument,
                got: snapshot.instrument,
            });
        }
        self.bids.clear();
        self.asks.clear();
        self.status = BookStatus::NeedsResync;
        let crossed = matches!(
            (snapshot.bids.first(), snapshot.asks.first()),
            (Some((b, _)), Some((a, _))) if b >= a
        );
        if crossed
            || !side_is_valid(&snapshot.bids, Side::Buy)
            || !side_is_valid(&snapshot.asks, Side::Sell)
        {
            return Err(BookError::InvalidSnapshot);
        }
        self.bids.extend_from_slice(&snapshot.bids);
        self.asks.extend_from_slice(&snapshot.asks);
        self.status = BookStatus::Synced;
        self.last_seq = snapshot.seq;
        self.last_update = snapshot.ts_local;
        Ok(())
    }

    /// Applies one delta. On error the levels are unchanged; any error other
    /// than [`BookError::WrongInstrument`] leaves the book in [`BookStatus::NeedsResync`].
    ///
    /// # Errors
    /// See [`BookError`].
    pub fn apply(&mut self, delta: &BookDelta) -> Result<(), BookError> {
        if delta.instrument != self.instrument {
            return Err(BookError::WrongInstrument {
                expected: self.instrument,
                got: delta.instrument,
            });
        }
        if self.status == BookStatus::NeedsResync {
            return Err(BookError::NeedsResync);
        }
        let expected = self.last_seq.wrapping_add(1);
        if delta.seq != expected {
            self.status = BookStatus::NeedsResync;
            return Err(BookError::SequenceGap {
                expected,
                got: delta.seq,
            });
        }
        if delta.qty < Qty::ZERO {
            self.status = BookStatus::NeedsResync;
            return Err(BookError::NegativeQty);
        }
        let (levels, opposite) = match delta.side {
            Side::Buy => (&mut self.bids, &self.asks),
            Side::Sell => (&mut self.asks, &self.bids),
        };
        match search(levels, delta.side, delta.price) {
            Ok(i) if delta.qty == Qty::ZERO => {
                levels.remove(i);
            }
            Ok(i) => levels[i].1 = delta.qty,
            Err(_) if delta.qty == Qty::ZERO => {}
            Err(i) => {
                if let Some((best_opposite, _)) = opposite.first()
                    && !better(delta.side, *best_opposite, delta.price)
                {
                    self.status = BookStatus::NeedsResync;
                    return Err(BookError::WouldCross {
                        side: delta.side,
                        price: delta.price,
                    });
                }
                if i < MAX_LEVELS {
                    levels.insert(i, (delta.price, delta.qty));
                    levels.truncate(MAX_LEVELS);
                }
            }
        }
        self.last_seq = delta.seq;
        self.last_update = delta.ts_local;
        Ok(())
    }

    #[must_use]
    pub fn status(&self) -> BookStatus {
        self.status
    }

    #[must_use]
    pub fn instrument(&self) -> InstrumentId {
        self.instrument
    }

    #[must_use]
    pub fn last_seq(&self) -> u64 {
        self.last_seq
    }

    /// Local receive time of the last snapshot or delta applied.
    #[must_use]
    pub fn last_update(&self) -> Timestamp {
        self.last_update
    }

    #[must_use]
    pub fn best_bid(&self) -> Option<(Price, Qty)> {
        self.bids.first().copied()
    }

    #[must_use]
    pub fn best_ask(&self) -> Option<(Price, Qty)> {
        self.asks.first().copied()
    }

    /// Levels on one side, best first.
    #[must_use]
    pub fn levels(&self, side: Side) -> &[(Price, Qty)] {
        match side {
            Side::Buy => &self.bids,
            Side::Sell => &self.asks,
        }
    }

    /// Midpoint rounded toward the bid, when both sides exist.
    #[must_use]
    pub fn mid(&self) -> Option<Price> {
        let (bid, _) = self.best_bid()?;
        let (ask, _) = self.best_ask()?;
        Some(Price::from_raw(bid.raw() + (ask.raw() - bid.raw()) / 2))
    }

    /// Checks every invariant in the module docs. Used by tests and fuzzing.
    #[must_use]
    pub fn invariants_hold(&self) -> bool {
        let uncrossed = match (self.best_bid(), self.best_ask()) {
            (Some((b, _)), Some((a, _))) => b < a,
            _ => true,
        };
        uncrossed && side_is_valid(&self.bids, Side::Buy) && side_is_valid(&self.asks, Side::Sell)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const I: InstrumentId = InstrumentId(1);

    fn delta(seq: u64, side: Side, price: i64, qty: i64) -> BookDelta {
        BookDelta {
            instrument: I,
            side,
            price: Price::from_raw(price),
            qty: Qty::from_raw(qty),
            seq,
            ts_exchange: Timestamp(seq),
            ts_local: Timestamp(seq),
        }
    }

    fn snapshot(seq: u64, bids: &[(i64, i64)], asks: &[(i64, i64)]) -> BookSnapshot {
        let conv = |v: &[(i64, i64)]| {
            v.iter()
                .map(|&(p, q)| (Price::from_raw(p), Qty::from_raw(q)))
                .collect()
        };
        BookSnapshot {
            instrument: I,
            seq,
            bids: conv(bids),
            asks: conv(asks),
            ts_exchange: Timestamp(seq),
            ts_local: Timestamp(seq),
        }
    }

    fn synced() -> OrderBook {
        let mut book = OrderBook::new(I);
        book.apply_snapshot(&snapshot(0, &[], &[])).unwrap();
        book
    }

    #[test]
    fn tracks_top_of_book_and_rejects_sequence_gaps() {
        let mut book = synced();
        book.apply(&delta(1, Side::Buy, 100, 5)).unwrap();
        book.apply(&delta(2, Side::Buy, 101, 3)).unwrap();
        book.apply(&delta(3, Side::Sell, 103, 2)).unwrap();
        book.apply(&delta(4, Side::Buy, 101, 0)).unwrap();
        assert_eq!(
            book.best_bid(),
            Some((Price::from_raw(100), Qty::from_raw(5)))
        );
        assert_eq!(
            book.best_ask(),
            Some((Price::from_raw(103), Qty::from_raw(2)))
        );
        assert_eq!(book.mid(), Some(Price::from_raw(101)));
        assert_eq!(
            book.apply(&delta(6, Side::Sell, 104, 1)),
            Err(BookError::SequenceGap {
                expected: 5,
                got: 6
            })
        );
        assert_eq!(book.status(), BookStatus::NeedsResync);
        assert_eq!(
            book.apply(&delta(5, Side::Sell, 104, 1)),
            Err(BookError::NeedsResync)
        );
    }

    #[test]
    fn new_book_needs_a_snapshot() {
        let mut book = OrderBook::new(I);
        assert_eq!(
            book.apply(&delta(1, Side::Buy, 100, 1)),
            Err(BookError::NeedsResync)
        );
    }

    #[test]
    fn crossing_or_locking_delta_is_rejected_and_forces_resync() {
        for price in [103, 104] {
            let mut book = synced();
            book.apply(&delta(1, Side::Buy, 100, 5)).unwrap();
            book.apply(&delta(2, Side::Sell, 103, 2)).unwrap();
            assert_eq!(
                book.apply(&delta(3, Side::Buy, price, 1)),
                Err(BookError::WouldCross {
                    side: Side::Buy,
                    price: Price::from_raw(price)
                })
            );
            assert_eq!(book.best_bid().unwrap().0, Price::from_raw(100));
            assert_eq!(book.status(), BookStatus::NeedsResync);
            assert!(book.invariants_hold());
        }
    }

    #[test]
    fn negative_qty_forces_resync() {
        let mut book = synced();
        assert_eq!(
            book.apply(&delta(1, Side::Buy, 100, -1)),
            Err(BookError::NegativeQty)
        );
        assert_eq!(book.status(), BookStatus::NeedsResync);
    }

    #[test]
    fn snapshot_resyncs_and_bad_snapshots_are_refused() {
        let mut book = synced();
        book.apply(&delta(5, Side::Buy, 1, 1)).unwrap_err();
        book.apply_snapshot(&snapshot(10, &[(100, 1), (99, 2)], &[(101, 1)]))
            .unwrap();
        book.apply(&delta(11, Side::Sell, 102, 1)).unwrap();
        assert_eq!(book.levels(Side::Sell).len(), 2);
        for bad in [
            snapshot(1, &[(100, 1)], &[(100, 1)]),
            snapshot(1, &[(99, 1), (100, 1)], &[]),
            snapshot(1, &[(99, 0)], &[]),
        ] {
            assert_eq!(book.apply_snapshot(&bad), Err(BookError::InvalidSnapshot));
            assert_eq!(book.status(), BookStatus::NeedsResync);
            assert!(book.best_bid().is_none());
        }
    }

    #[test]
    fn depth_is_capped_by_dropping_the_worst_levels() {
        let mut book = synced();
        let base = 1_000_000;
        for (seq, i) in (1..).zip(0..=i64::try_from(MAX_LEVELS).unwrap()) {
            book.apply(&delta(seq, Side::Buy, base - i, 1)).unwrap();
        }
        assert_eq!(book.levels(Side::Buy).len(), MAX_LEVELS);
        assert_eq!(book.best_bid().unwrap().0, Price::from_raw(base));
        assert!(book.invariants_hold());
    }
}
