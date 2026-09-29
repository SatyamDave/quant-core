//! `SimVenue` against a recorded fixture: every event it emits is pinned.

use qc_core::{
    ClientOrderId, Event, InstrumentId, OrderEvent, OrderRequest, OrderType, Price, Qty, Side,
    StrategyId, Timestamp, VenueId, VenueOrderId,
};
use qc_gateway::{GatewayError, SimVenue, VenueAdapter, record};

fn p(s: &str) -> Price {
    s.parse().unwrap()
}
fn q(s: &str) -> Qty {
    s.parse().unwrap()
}

fn order(id: u64, side: Side, order_type: OrderType, price: &str, qty: &str) -> OrderRequest {
    OrderRequest {
        client_id: ClientOrderId(id),
        strategy: StrategyId(1),
        instrument: InstrumentId(1),
        side,
        order_type,
        price: p(price),
        qty: q(qty),
    }
}

fn drain(v: &mut SimVenue) -> Vec<OrderEvent> {
    std::iter::from_fn(|| v.poll())
        .map(|e| match e {
            Event::Order(o) => o,
            Event::Market(m) => panic!("sim venue only emits order events, got {m:?}"),
        })
        .collect()
}

#[test]
#[expect(
    clippy::too_many_lines,
    reason = "one scripted session, read top to bottom"
)]
fn sim_venue_replays_the_fixture_session_exactly() {
    let records = record::parse(include_str!("fixtures/sim_session.csv")).unwrap();
    let mut v = SimVenue::new(VenueId(1), InstrumentId(1));
    let c = ClientOrderId;
    let t = Timestamp(1000);

    v.on_record(&records[0]);
    v.submit(&order(1, Side::Buy, OrderType::Limit, "100.2", "1.5"))
        .unwrap();
    assert_eq!(
        drain(&mut v),
        [
            OrderEvent::Accepted {
                client_id: c(1),
                venue_id: VenueOrderId(1),
                ts: t
            },
            OrderEvent::Filled {
                client_id: c(1),
                price: p("100.1"),
                qty: q("1"),
                ts: t
            },
            OrderEvent::Filled {
                client_id: c(1),
                price: p("100.2"),
                qty: q("0.5"),
                ts: t
            },
        ]
    );

    v.submit(&order(2, Side::Sell, OrderType::PostOnly, "100", "1"))
        .unwrap();
    v.submit(&order(3, Side::Buy, OrderType::Limit, "99.95", "0.5"))
        .unwrap();
    v.submit(&order(4, Side::Sell, OrderType::Ioc, "99.95", "3"))
        .unwrap();
    assert_eq!(
        drain(&mut v),
        [
            OrderEvent::Rejected {
                client_id: c(2),
                ts: t
            },
            OrderEvent::Accepted {
                client_id: c(3),
                venue_id: VenueOrderId(2),
                ts: t
            },
            OrderEvent::Accepted {
                client_id: c(4),
                venue_id: VenueOrderId(3),
                ts: t
            },
            OrderEvent::Filled {
                client_id: c(4),
                price: p("100"),
                qty: q("2"),
                ts: t
            },
            OrderEvent::Canceled {
                client_id: c(4),
                ts: t
            },
        ]
    );

    // A sell trade at 99.9 prints through the resting 99.95 bid: partial fill.
    v.on_record(&records[1]);
    let t2 = Timestamp(2000);
    assert_eq!(
        drain(&mut v),
        [OrderEvent::Filled {
            client_id: c(3),
            price: p("99.95"),
            qty: q("0.2"),
            ts: t2
        }]
    );

    v.cancel(c(3)).unwrap();
    v.cancel(c(3)).unwrap();
    assert_eq!(
        drain(&mut v),
        [
            OrderEvent::Canceled {
                client_id: c(3),
                ts: t2
            },
            OrderEvent::CancelRejected {
                client_id: c(3),
                ts: t2
            },
        ]
    );

    v.on_record(&records[2]);
    assert_eq!(v.book().best_ask(), Some((p("100.2"), q("3"))));

    v.set_connected(false);
    assert_eq!(
        v.submit(&order(5, Side::Buy, OrderType::Limit, "99", "1")),
        Err(GatewayError::Disconnected)
    );
    assert_eq!(v.cancel(c(1)), Err(GatewayError::Disconnected));
    v.set_connected(true);

    let snapshot = v.order_snapshot().unwrap();
    let summary: Vec<_> = snapshot
        .iter()
        .map(|o| (o.client_id.unwrap().0, o.open, o.filled))
        .collect();
    assert_eq!(
        summary,
        [
            (1, false, q("1.5")),
            (3, false, q("0.2")),
            (4, false, q("2"))
        ]
    );
}

/// The old bug: `SimVenue` never reduced the displayed size a fill took from,
/// so a second aggressive order arriving before the next book update could
/// take the same quantity again ("double-take"). This must fail before the
/// fix that tracks consumed size per level.
#[test]
fn aggressive_orders_consume_displayed_liquidity_so_a_second_cannot_double_take() {
    let records = record::parse("S,1,1,1000,1000,,100@2\n").unwrap();
    let mut v = SimVenue::new(VenueId(1), InstrumentId(1));
    let c = ClientOrderId;
    let t = Timestamp(1000);

    v.on_record(&records[0]);
    v.submit(&order(1, Side::Buy, OrderType::Ioc, "100", "2"))
        .unwrap();
    assert_eq!(
        drain(&mut v),
        [
            OrderEvent::Accepted {
                client_id: c(1),
                venue_id: VenueOrderId(1),
                ts: t
            },
            OrderEvent::Filled {
                client_id: c(1),
                price: p("100"),
                qty: q("2"),
                ts: t
            },
        ]
    );

    // Same displayed level, no book update in between: nothing is left to
    // take, so this order gets no fill and its IOC remainder is canceled.
    v.submit(&order(2, Side::Buy, OrderType::Ioc, "100", "2"))
        .unwrap();
    assert_eq!(
        drain(&mut v),
        [
            OrderEvent::Accepted {
                client_id: c(2),
                venue_id: VenueOrderId(2),
                ts: t
            },
            OrderEvent::Canceled {
                client_id: c(2),
                ts: t
            },
        ]
    );
}

/// A level that trades all the way down still fills in full once the feed
/// republishes it: the next book update that touches a level replaces
/// whatever was tracked as consumed from it.
#[test]
fn a_book_update_that_replaces_the_level_resets_what_was_consumed_from_it() {
    let records = record::parse("S,1,1,1000,1000,,100@2\nD,1,2,S,100,2,2000,2000\n").unwrap();
    let mut v = SimVenue::new(VenueId(1), InstrumentId(1));
    let c = ClientOrderId;
    let t1 = Timestamp(1000);
    let t2 = Timestamp(2000);

    v.on_record(&records[0]);
    v.submit(&order(1, Side::Buy, OrderType::Ioc, "100", "2"))
        .unwrap();
    assert_eq!(
        drain(&mut v),
        [
            OrderEvent::Accepted {
                client_id: c(1),
                venue_id: VenueOrderId(1),
                ts: t1
            },
            OrderEvent::Filled {
                client_id: c(1),
                price: p("100"),
                qty: q("2"),
                ts: t1
            },
        ]
    );

    // The feed republishes the level (still showing 2): a fresh view, so the
    // next aggressive order can take the full displayed size again.
    v.on_record(&records[1]);
    v.submit(&order(2, Side::Buy, OrderType::Ioc, "100", "2"))
        .unwrap();
    assert_eq!(
        drain(&mut v),
        [
            OrderEvent::Accepted {
                client_id: c(2),
                venue_id: VenueOrderId(2),
                ts: t2
            },
            OrderEvent::Filled {
                client_id: c(2),
                price: p("100"),
                qty: q("2"),
                ts: t2
            },
        ]
    );
}

/// The old bug: `SimVenue` ignored queue position, so a resting order filled
/// the instant any qualifying trade printed, regardless of how much size was
/// already displayed ahead of it. This must fail before the fix.
#[test]
fn resting_orders_queue_behind_displayed_size_and_fill_only_once_it_trades_through() {
    let records = record::parse("S,1,1,1000,1000,,100@3\n").unwrap();
    let mut v = SimVenue::new(VenueId(1), InstrumentId(1));
    let c = ClientOrderId;
    let t1 = Timestamp(1000);
    let trade =
        |ts: u64, qty: &str| record::parse(&format!("T,1,B,100,{qty},{ts},{ts}\n")).unwrap();

    v.on_record(&records[0]);
    // Rests behind the 3 already displayed at 100; nothing crosses (no bids).
    v.submit(&order(1, Side::Sell, OrderType::Limit, "100", "2"))
        .unwrap();
    assert_eq!(
        drain(&mut v),
        [OrderEvent::Accepted {
            client_id: c(1),
            venue_id: VenueOrderId(1),
            ts: t1
        }]
    );

    // 2 units trade through: only depletes the queue ahead (3 -> 1), no fill.
    v.on_record(&trade(2000, "2")[0]);
    assert_eq!(drain(&mut v), []);

    // 2 more units: 1 clears the remaining queue, the other 1 fills us
    // (partial fill: our qty is 2, only 1 is filled so far).
    v.on_record(&trade(3000, "2")[0]);
    assert_eq!(
        drain(&mut v),
        [OrderEvent::Filled {
            client_id: c(1),
            price: p("100"),
            qty: q("1"),
            ts: Timestamp(3000)
        }]
    );

    // A further trade fills the remainder.
    v.on_record(&trade(4000, "5")[0]);
    assert_eq!(
        drain(&mut v),
        [OrderEvent::Filled {
            client_id: c(1),
            price: p("100"),
            qty: q("1"),
            ts: Timestamp(4000)
        }]
    );

    let snapshot = v.order_snapshot().unwrap();
    let summary: Vec<_> = snapshot
        .iter()
        .map(|o| (o.client_id.unwrap().0, o.open, o.filled))
        .collect();
    assert_eq!(summary, [(1, false, q("2"))]);
}
