use criterion::{Criterion, criterion_group, criterion_main};
use qc_core::{BookDelta, InstrumentId, Price, Qty, Side, Timestamp};
use qc_orderbook::{BookSnapshot, OrderBook};

/// 20 levels a side around 1000/1001; deltas change, remove and re-add levels
/// on their own side only, so the book never crosses.
fn apply_deltas(c: &mut Criterion) {
    let level = |p: i64| (Price::from_raw(p), Qty::from_raw(10));
    let snapshot = BookSnapshot {
        instrument: InstrumentId(1),
        seq: 0,
        bids: (0..20).map(|i| level(1000 - i)).collect(),
        asks: (0..20).map(|i| level(1001 + i)).collect(),
        ts_exchange: Timestamp(0),
        ts_local: Timestamp(0),
    };
    let deltas: Vec<BookDelta> = (1..=1_000_u64)
        .map(|seq| {
            let buy = seq % 2 == 0;
            let offset = i64::try_from(seq % 20).unwrap();
            BookDelta {
                instrument: InstrumentId(1),
                side: if buy { Side::Buy } else { Side::Sell },
                price: Price::from_raw(if buy { 1000 - offset } else { 1001 + offset }),
                qty: Qty::from_raw(i64::try_from(seq % 7).unwrap()),
                seq,
                ts_exchange: Timestamp(seq),
                ts_local: Timestamp(seq),
            }
        })
        .collect();
    let mut book = OrderBook::new(InstrumentId(1));
    c.bench_function("orderbook_apply_1000", |b| {
        b.iter(|| {
            book.apply_snapshot(&snapshot).unwrap();
            for d in &deltas {
                book.apply(d).unwrap();
            }
            std::hint::black_box(book.best_bid())
        });
    });
}

criterion_group!(benches, apply_deltas);
criterion_main!(benches);
