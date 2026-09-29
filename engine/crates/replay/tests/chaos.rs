//! Failure injection: kill switch from another thread, venue disconnect, stale
//! book, partial fills, an order the process never sent, an unreachable venue
//! at reconcile time, and the daily loss limit.

use std::thread;
use std::time::{Duration, Instant};

use qc_core::{
    ClientOrderId, Event, InstrumentId, OrderRequest, OrderType, Price, Qty, Side, StrategyId,
    VenueId,
};
use qc_gateway::{Record, SimVenue, record};
use qc_oms::OrderState;
use qc_orderbook::OrderBook;
use qc_replay::{Engine, HaltReason, SAMPLE_INSTRUMENT, sample_engine};
use qc_risk::{KillSwitch, Limits, RiskEngine};
use qc_strategy_runtime::{Command, InsideQuoter, Strategy};

type Sample = Engine<InsideQuoter, SimVenue>;

fn limits() -> Limits {
    toml::from_str(include_str!("../../../../config/limits/default.toml")).unwrap()
}

fn feed(engine: &mut Sample, records: &[Record]) {
    for r in records {
        engine.venue_mut().on_record(r);
        engine.on_record(r);
    }
}

fn parse(text: &str) -> Vec<Record> {
    record::parse(text).unwrap()
}

const MS: u64 = 1_000_000;

/// Book at bid 100.0 / ask 100.2, sequence 10, at `t` ms.
fn open_book(t: u64) -> Vec<Record> {
    let ns = t * MS;
    parse(&format!("S,1,10,{ns},{ns},100.0@1;99.9@1,100.2@1;100.3@1"))
}

/// A bid appears at 100.1 or disappears again: the quoter's bid moves each time.
fn flicker(i: u64) -> Record {
    let ns = 1000 * MS + i * 250 * MS;
    let qty = if i % 2 == 0 { "1" } else { "0" };
    parse(&format!("D,1,{},B,100.1,{qty},{ns},{ns}", 11 + i)).remove(0)
}

#[test]
fn kill_switch_from_another_thread_halts_within_one_second() {
    let kill = KillSwitch::new();
    let remote = kill.clone();
    let mut engine = sample_engine(limits(), kill);
    feed(&mut engine, &open_book(0));

    let flip_thread = thread::spawn(move || {
        thread::sleep(Duration::from_millis(50));
        let flipped = Instant::now();
        remote.engage();
        flipped
    });

    let started = Instant::now();
    let mut i = 0;
    let (observed, submitted_at_halt) = loop {
        feed(&mut engine, &[flicker(i)]);
        i += 1;
        if engine.halted().is_some() {
            break (Instant::now(), engine.submitted());
        }
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "kill switch never observed"
        );
    };
    let flipped = flip_thread.join().unwrap();

    let latency = observed.duration_since(flipped);
    println!("kill switch observed {latency:?} after it was engaged ({i} records replayed)");
    assert!(latency < Duration::from_secs(1));
    assert_eq!(engine.halted(), Some(HaltReason::KillSwitch));
    assert!(submitted_at_halt > 2, "orders were flowing before the flip");

    // Keep feeding: nothing more may be submitted, and every order is being canceled.
    for j in i..i + 1000 {
        feed(&mut engine, &[flicker(j)]);
    }
    assert_eq!(engine.submitted(), submitted_at_halt);
    let after_halt = engine.log().split_once(" halt KillSwitch").unwrap().1;
    assert!(
        !after_halt.contains(" submit "),
        "submit after halt:\n{after_halt}"
    );
    assert_eq!(engine.oms().open_orders().count(), 0);
}

#[test]
fn disconnect_rejects_new_orders_locally_and_trading_resumes_after_reconnect() {
    let mut engine = sample_engine(limits(), KillSwitch::new());
    feed(&mut engine, &open_book(0));
    feed(&mut engine, &[flicker(0)]);
    let before = engine.submitted();
    assert!(before > 0);

    engine.venue_mut().set_connected(false);
    for i in 1..5 {
        feed(&mut engine, &[flicker(i)]);
    }
    assert_eq!(
        engine.submitted(),
        before,
        "nothing reaches a disconnected venue"
    );
    assert!(engine.log().contains("cancel-failed"), "{}", engine.log());
    assert!(
        engine
            .oms()
            .open_orders()
            .all(|o| o.state != OrderState::PendingNew),
        "a failed submit must not leave an order pending"
    );

    engine.venue_mut().set_connected(true);
    for i in 5..10 {
        feed(&mut engine, &[flicker(i)]);
    }
    assert!(
        engine.submitted() > before,
        "trading resumes after reconnect"
    );
    engine.reconcile();
    assert_eq!(engine.halted(), None, "{}", engine.log());
}

#[test]
fn stale_book_blocks_new_orders_until_fresh_data_arrives() {
    let mut engine = sample_engine(limits(), KillSwitch::new());
    feed(&mut engine, &open_book(0));
    // First market event places quotes; a sell trade 1.5 s later fills the bid
    // (1.0 already displayed at 100.0 is ahead of our quote in the queue, so the
    // trade has to clear that before it reaches us), then another trade asks the
    // quoter to replace it on a book that is now stale.
    feed(
        &mut engine,
        &parse(&format!(
            "T,1,B,100.2,0.0001,{a},{a}\nT,1,S,100.0,1.001,{b},{b}\nT,1,S,100.0,0.0001,{c},{c}",
            a = 10 * MS,
            b = 1500 * MS,
            c = 1600 * MS
        )),
    );
    assert!(engine.log().contains("StaleData"), "{}", engine.log());
    let submitted = engine.submitted();
    feed(
        &mut engine,
        &parse(&format!("D,1,11,B,99.8,1,{t},{t}", t = 1700 * MS)),
    );
    assert_eq!(
        engine.submitted(),
        submitted + 1,
        "fresh data unblocks the bid"
    );
}

#[test]
fn partial_fills_accumulate_without_overfilling() {
    let mut engine = sample_engine(limits(), KillSwitch::new());
    feed(&mut engine, &open_book(0));
    let t = |ms: u64| ms * MS;
    // 1.0 is already displayed at 100.0 ahead of our bid in the queue; the
    // second trade clears that (1.0) and then partially fills us for 0.0004.
    feed(
        &mut engine,
        &parse(&format!(
            "T,1,B,100.2,0.0001,{},{}\nT,1,S,100.0,1.0004,{},{}",
            t(10),
            t(10),
            t(20),
            t(20)
        )),
    );
    let bid = engine.oms().get(ClientOrderId(1)).unwrap();
    assert_eq!(
        (bid.request.side, bid.state),
        (Side::Buy, OrderState::PartiallyFilled)
    );
    assert_eq!(engine.position(), "0.0004".parse::<Qty>().unwrap());

    feed(
        &mut engine,
        &parse(&format!("T,1,S,100.0,0.5,{},{}", t(30), t(30))),
    );
    assert_eq!(
        engine.oms().get(ClientOrderId(1)).unwrap().state,
        OrderState::Filled
    );
    assert_eq!(engine.position(), "0.001".parse::<Qty>().unwrap());
    assert!(!engine.log().contains("Overfill"));
}

#[test]
fn unknown_order_at_the_venue_halts_and_cancels_everything() {
    let mut engine = sample_engine(limits(), KillSwitch::new());
    feed(&mut engine, &open_book(0));
    feed(&mut engine, &[flicker(0)]);
    engine
        .venue_mut()
        .inject_foreign_order(Side::Buy, Price::from_raw(1), Qty::from_raw(1));
    engine.reconcile();
    assert_eq!(engine.halted(), Some(HaltReason::Reconciliation));
    let submitted = engine.submitted();
    feed(&mut engine, &[flicker(1), flicker(2)]);
    assert_eq!(engine.submitted(), submitted);
    assert_eq!(engine.oms().open_orders().count(), 0);
}

/// On each market event, submits the orders `plan` derives from the book, with no
/// self-protection, so the engine and risk are the only guards.
struct Scripted {
    plan: fn(&OrderBook) -> Vec<(Side, OrderType, Price)>,
    next_id: u64,
}

impl Strategy for Scripted {
    fn on_event(&mut self, event: &Event, book: &OrderBook, out: &mut Vec<Command>) {
        if !matches!(event, Event::Market(_)) {
            return;
        }
        for (side, order_type, price) in (self.plan)(book) {
            self.next_id += 1;
            out.push(Command::Submit(OrderRequest {
                client_id: ClientOrderId(self.next_id),
                strategy: StrategyId(2),
                instrument: SAMPLE_INSTRUMENT,
                side,
                order_type,
                price,
                qty: "0.001".parse().unwrap(),
            }));
        }
    }
}

fn scripted(
    limits: Limits,
    plan: fn(&OrderBook) -> Vec<(Side, OrderType, Price)>,
) -> Engine<Scripted, SimVenue> {
    Engine::new(
        SAMPLE_INSTRUMENT,
        Scripted { plan, next_id: 0 },
        RiskEngine::new(limits, KillSwitch::new()),
        SimVenue::new(VenueId(1), InstrumentId(1)),
    )
}

fn feed_scripted(engine: &mut Engine<Scripted, SimVenue>, records: &[Record]) {
    for r in records {
        engine.venue_mut().on_record(r);
        engine.on_record(r);
    }
}

fn open_at_venue(venue: &SimVenue) -> usize {
    venue.orders().iter().filter(|o| o.open).count()
}

#[test]
fn kill_switch_during_a_disconnect_cancels_every_order_once_the_venue_is_back() {
    let kill = KillSwitch::new();
    let mut engine = sample_engine(limits(), kill.clone());
    feed(&mut engine, &open_book(0));
    feed(&mut engine, &[flicker(0)]);
    assert!(open_at_venue(engine.venue_mut()) > 0, "quotes rest");

    engine.venue_mut().set_connected(false);
    kill.engage();
    feed(&mut engine, &[flicker(1), flicker(2)]);
    assert_eq!(engine.halted(), Some(HaltReason::KillSwitch));
    let log = engine.log().to_owned();
    assert!(log.contains("cancel-failed"), "{log}");
    let after_halt = log.split_once(" halt KillSwitch").unwrap().1;
    assert!(
        !after_halt
            .lines()
            .any(|l| l.split(' ').nth(1) == Some("cancel")),
        "a cancel the venue never received is logged as sent:\n{after_halt}"
    );

    engine.venue_mut().set_connected(true);
    feed(&mut engine, &[flicker(3), flicker(4)]);
    assert_eq!(open_at_venue(engine.venue_mut()), 0, "{}", engine.log());
    assert_eq!(engine.oms().open_orders().count(), 0);
}

#[test]
fn a_book_awaiting_resync_is_no_reference_price() {
    let bid_at_mid = |book: &OrderBook| {
        book.mid()
            .map(|m| (Side::Buy, OrderType::Limit, m))
            .into_iter()
            .collect()
    };
    let mut engine = scripted(limits(), bid_at_mid);
    feed_scripted(&mut engine, &open_book(0));
    feed_scripted(
        &mut engine,
        &parse(&format!("D,1,11,B,99.9,2,{t},{t}", t = 50 * MS)),
    );
    assert_eq!(engine.submitted(), 1, "a synced book trades");

    // Sequence 13 skips 12: the book keeps its levels but needs a snapshot.
    feed_scripted(
        &mut engine,
        &parse(&format!(
            "D,1,13,B,99.9,3,{a},{a}\nT,1,B,100.2,0.0001,{b},{b}",
            a = 100 * MS,
            b = 200 * MS
        )),
    );
    assert!(engine.log().contains("SequenceGap"));
    assert_eq!(engine.submitted(), 1, "{}", engine.log());
    assert!(engine.log().contains("PriceBand"), "{}", engine.log());
}

#[test]
fn no_order_is_sent_before_the_first_successful_reconcile() {
    let mut engine = sample_engine(limits(), KillSwitch::new());
    engine.venue_mut().set_connected(false);
    feed(&mut engine, &open_book(0));
    for i in 0..3 {
        feed(&mut engine, &[flicker(i)]);
    }
    assert_eq!(engine.submitted(), 0);
    assert!(
        !engine.log().contains("gateway"),
        "an order reached the gateway before any reconcile:\n{}",
        engine.log()
    );
    assert!(
        engine.log().contains("reconcile failed"),
        "{}",
        engine.log()
    );

    engine.venue_mut().set_connected(true);
    for i in 3..6 {
        feed(&mut engine, &[flicker(i)]);
    }
    assert!(engine.submitted() > 0, "{}", engine.log());
    assert_eq!(engine.halted(), None);
}

#[test]
fn a_failed_reconcile_blocks_orders_until_one_succeeds() {
    let mut engine = sample_engine(limits(), KillSwitch::new());
    feed(&mut engine, &open_book(0));
    feed(&mut engine, &[flicker(0), flicker(1)]);
    assert!(engine.submitted() > 0);

    // flicker(236) is at 60 s, when the periodic reconcile is due.
    engine.venue_mut().set_connected(false);
    for i in 2..=236 {
        feed(&mut engine, &[flicker(i)]);
    }
    assert!(
        engine.log().contains("reconcile failed"),
        "{}",
        engine.log()
    );

    engine.venue_mut().set_connected(true);
    let before = engine.submitted();
    feed(&mut engine, &[flicker(237)]);
    assert_eq!(
        engine.submitted(),
        before,
        "orders went out before a reconcile succeeded"
    );
    feed(&mut engine, &[flicker(238), flicker(239)]);
    assert!(
        engine.submitted() > before,
        "trading resumes once reconciled"
    );
    assert_eq!(engine.halted(), None, "{}", engine.log());
}

#[test]
fn reaching_the_daily_loss_limit_on_a_fill_halts_and_cancels_working_orders() {
    // A resting bid below the market, and a buy that lifts the ask at 100.2
    // against a mid of 100.1: a loss of 0.0001 the moment it fills.
    let rest_and_take = |book: &OrderBook| match (book.best_bid(), book.best_ask()) {
        (Some((bid, _)), Some((ask, _))) => vec![
            (Side::Buy, OrderType::PostOnly, bid),
            (Side::Buy, OrderType::Limit, ask),
        ],
        _ => Vec::new(),
    };
    let mut limits = limits();
    limits.max_daily_loss = "0.0001".parse().unwrap();
    let mut engine = scripted(limits, rest_and_take);
    feed_scripted(&mut engine, &open_book(0));
    feed_scripted(
        &mut engine,
        &parse(&format!("D,1,11,B,99.9,2,{t},{t}", t = 50 * MS)),
    );
    assert!(engine.log().contains("Filled"), "{}", engine.log());
    assert_eq!(
        engine.halted(),
        Some(HaltReason::MaxDailyLoss),
        "{}",
        engine.log()
    );
    assert_eq!(open_at_venue(engine.venue_mut()), 0, "{}", engine.log());
    assert_eq!(engine.oms().open_orders().count(), 0);

    let submitted = engine.submitted();
    feed_scripted(
        &mut engine,
        &parse(&format!("D,1,12,B,99.9,3,{t},{t}", t = 60 * MS)),
    );
    assert_eq!(engine.submitted(), submitted);
}
