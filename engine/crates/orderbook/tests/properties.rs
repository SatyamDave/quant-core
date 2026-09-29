//! Property tests: random delta streams never leave the book crossed, and a
//! sequence gap is always reported.

use std::collections::BTreeMap;

use proptest::prelude::*;
use qc_core::{BookDelta, InstrumentId, Price, Qty, Side, Timestamp};
use qc_orderbook::{BookError, BookSnapshot, BookStatus, OrderBook};

const I: InstrumentId = InstrumentId(1);

/// `gap` is how far past the expected sequence number the delta lands:
/// 0 is in order, anything else is a gap (or a repeat, via wrapping).
#[derive(Debug, Clone, Copy)]
struct Step {
    buy: bool,
    price: i64,
    qty: i64,
    gap: u64,
}

fn step() -> impl Strategy<Value = Step> {
    (
        any::<bool>(),
        90_i64..110,
        0_i64..4,
        prop_oneof![8 => Just(0_u64), 1 => 1_u64..5, 1 => Just(u64::MAX)],
    )
        .prop_map(|(buy, price, qty, gap)| Step {
            buy,
            price,
            qty,
            gap,
        })
}

fn snapshot_of(seq: u64, bids: &BTreeMap<i64, i64>, asks: &BTreeMap<i64, i64>) -> BookSnapshot {
    let conv = |it: &mut dyn Iterator<Item = (&i64, &i64)>| -> Vec<(Price, Qty)> {
        it.map(|(p, q)| (Price::from_raw(*p), Qty::from_raw(*q)))
            .collect()
    };
    BookSnapshot {
        instrument: I,
        seq,
        bids: conv(&mut bids.iter().rev()),
        asks: conv(&mut asks.iter()),
        ts_exchange: Timestamp(seq),
        ts_local: Timestamp(seq),
    }
}

proptest! {
    #[test]
    fn random_streams_never_cross_and_gaps_are_detected(steps in prop::collection::vec(step(), 1..400)) {
        let mut book = OrderBook::new(I);
        // Reference model: plain maps holding what the book should contain.
        let mut bids = BTreeMap::new();
        let mut asks = BTreeMap::new();
        book.apply_snapshot(&snapshot_of(0, &bids, &asks)).unwrap();

        for s in steps {
            let expected = book.last_seq() + 1;
            let seq = expected.wrapping_add(s.gap);
            let side = if s.buy { Side::Buy } else { Side::Sell };
            let delta = BookDelta {
                instrument: I,
                side,
                price: Price::from_raw(s.price),
                qty: Qty::from_raw(s.qty),
                seq,
                ts_exchange: Timestamp(seq),
                ts_local: Timestamp(seq),
            };
            let result = book.apply(&delta);
            prop_assert!(book.invariants_hold(), "invariants broken after {delta:?}");

            if s.gap != 0 {
                prop_assert_eq!(result.clone(), Err(BookError::SequenceGap { expected, got: seq }));
            }
            match result {
                Ok(()) => {
                    let model = if s.buy { &mut bids } else { &mut asks };
                    if s.qty == 0 { model.remove(&s.price); } else { model.insert(s.price, s.qty); }
                }
                Err(BookError::SequenceGap { .. } | BookError::WouldCross { .. }) => {
                    prop_assert_eq!(book.status(), BookStatus::NeedsResync);
                    prop_assert_eq!(book.apply(&BookDelta { seq: expected, ..delta }), Err(BookError::NeedsResync));
                    // Resync from the reference model at the sequence the feed is now at.
                    book.apply_snapshot(&snapshot_of(seq, &bids, &asks)).unwrap();
                }
                Err(other) => prop_assert!(false, "unexpected error {other:?}"),
            }
            let levels = |side, model: &BTreeMap<i64, i64>| -> Vec<(i64, i64)> {
                let mut v: Vec<_> = model.iter().map(|(p, q)| (*p, *q)).collect();
                if side == Side::Buy { v.reverse(); }
                let got: Vec<_> = book.levels(side).iter().map(|(p, q)| (p.raw(), q.raw())).collect();
                assert_eq!(got, v);
                got
            };
            levels(Side::Buy, &bids);
            levels(Side::Sell, &asks);
        }
    }
}
