//! The engine loop, driven by recorded market data: book → strategy → risk →
//! OMS → venue, and venue events back through the OMS to the strategy. Rule 7:
//! the same recording must produce the same order log, byte for byte.

use std::collections::VecDeque;
use std::fmt::Write as _;

use qc_core::{
    ClientOrderId, Clock, Event, InstrumentId, MarketEvent, OrderEvent, OrderRequest, Price, Qty,
    SCALE, Side, SimClock, StrategyId, Timestamp, VenueId,
};
use qc_gateway::{Record, SimVenue, VenueAdapter};
use qc_oms::{Oms, OrderState, reconcile};
use qc_orderbook::{BookStatus, OrderBook};
use qc_risk::{KillSwitch, Limits, RiskCheck, RiskContext, RiskEngine, RiskReject};
use qc_strategy_runtime::{Command, InsideQuoter, Strategy};

/// How often the engine reconciles with the venue, in simulated time.
pub const RECONCILE_EVERY_NS: u64 = 60 * 1_000_000_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HaltReason {
    KillSwitch,
    /// Realized plus unrealized loss reached `max_daily_loss`.
    MaxDailyLoss,
    /// The venue sent an event the order state machine does not allow.
    IllegalOrderEvent,
    /// Reconciliation found an unknown venue order or a mismatch.
    Reconciliation,
    /// One record set off more than [`MAX_EVENTS_PER_RECORD`] follow-up events,
    /// e.g. a strategy resubmitting after every reject.
    Runaway,
}

/// Follow-up order events one record may cause before the engine halts.
pub const MAX_EVENTS_PER_RECORD: u32 = 10_000;

pub struct Engine<S, V> {
    book: OrderBook,
    strategy: S,
    risk: RiskEngine,
    oms: Oms,
    venue: V,
    clock: SimClock,
    position: Qty,
    /// Quote-currency cash from fills, in raw [`Price`] units, wide so it cannot overflow.
    cash: i128,
    halted: Option<HaltReason>,
    /// False until a reconcile succeeds and again after any fails; no order is sent meanwhile.
    reconciled: bool,
    next_reconcile: Option<Timestamp>,
    submitted: u64,
    commands: Vec<Command>,
    feedback: VecDeque<OrderEvent>,
    // ponytail: a text log is fine for replay and tests; a live engine writes a binary journal off the hot path.
    log: String,
}

impl<S: Strategy, V: VenueAdapter> Engine<S, V> {
    #[must_use]
    pub fn new(instrument: InstrumentId, strategy: S, risk: RiskEngine, venue: V) -> Self {
        Self {
            book: OrderBook::new(instrument),
            strategy,
            risk,
            oms: Oms::default(),
            venue,
            clock: SimClock::default(),
            position: Qty::ZERO,
            cash: 0,
            halted: None,
            reconciled: false,
            next_reconcile: None,
            submitted: 0,
            commands: Vec::with_capacity(16),
            feedback: VecDeque::with_capacity(16),
            log: String::new(),
        }
    }

    pub fn venue_mut(&mut self) -> &mut V {
        &mut self.venue
    }

    #[must_use]
    pub fn halted(&self) -> Option<HaltReason> {
        self.halted
    }

    /// Orders handed to the venue so far.
    #[must_use]
    pub fn submitted(&self) -> u64 {
        self.submitted
    }

    #[must_use]
    pub fn position(&self) -> Qty {
        self.position
    }

    #[must_use]
    pub fn oms(&self) -> &Oms {
        &self.oms
    }

    #[must_use]
    pub fn log(&self) -> &str {
        &self.log
    }

    fn line(&mut self, args: std::fmt::Arguments<'_>) {
        let _ = writeln!(self.log, "{} {args}", self.clock.now().0);
    }

    /// Realized plus unrealized PnL at the current mid (or zero-marked without one).
    #[must_use]
    pub fn daily_pnl(&self) -> Price {
        let mark = self.book.mid().map_or(0, |m| i128::from(m.raw()));
        let pnl = self.cash + i128::from(self.position.raw()) * mark / i128::from(SCALE);
        Price::from_raw(i64::try_from(pnl).unwrap_or(if pnl < 0 { i64::MIN } else { i64::MAX }))
    }

    /// Processes one recorded record at its local timestamp.
    pub fn on_record(&mut self, record: &Record) {
        self.clock.advance_to(record.ts_local());
        if self.halted.is_some() {
            self.cancel_all_open();
        }
        self.check_kill_switch();
        let result = match record {
            Record::Snapshot(s) => self.book.apply_snapshot(s),
            Record::Market(MarketEvent::BookDelta(d)) => self.book.apply(d),
            Record::Market(MarketEvent::Trade(_)) => Ok(()),
        };
        if let Err(e) = result {
            self.line(format_args!("book {e:?}"));
        }
        self.check_daily_loss();
        if self.next_reconcile.is_none() {
            // Startup: nothing has been sent, so nothing is in flight to disagree about.
            self.reconcile();
        }
        if let Record::Market(m) = record {
            self.dispatch(&Event::Market(*m));
        }
        self.drain();
        let due = self.next_reconcile.is_some_and(|t| self.clock.now() >= t);
        if due || !self.reconciled {
            self.reconcile();
        }
    }

    /// Compares the OMS with the venue; any discrepancy halts. Order fills are
    /// compared, positions are not: the adapter has no position endpoint yet.
    pub fn reconcile(&mut self) {
        self.next_reconcile = Some(Timestamp(self.clock.now().0 + RECONCILE_EVERY_NS));
        let venue_orders = match self.venue.order_snapshot() {
            Ok(v) => v,
            Err(e) => {
                self.reconciled = false;
                self.line(format_args!("reconcile failed {e:?}"));
                return;
            }
        };
        let r = reconcile(&self.oms, &venue_orders);
        if r.halt() {
            self.line(format_args!("reconcile {:?}", r.discrepancies));
            self.halt(HaltReason::Reconciliation);
        }
        self.reconciled = true;
    }

    fn check_daily_loss(&mut self) {
        if self.halted.is_none() && self.risk.daily_loss_reached(self.daily_pnl()) {
            self.halt(HaltReason::MaxDailyLoss);
        }
    }

    fn check_kill_switch(&mut self) {
        if self.halted.is_none() && self.risk.kill_switch().is_engaged() {
            self.halt(HaltReason::KillSwitch);
        }
    }

    /// Stops all order entry for good and cancels every working order. Cancels
    /// the venue could not take are sent again on every record until none is open.
    fn halt(&mut self, reason: HaltReason) {
        if self.halted.is_some() {
            return;
        }
        self.halted = Some(reason);
        self.line(format_args!("halt {reason:?}"));
        self.cancel_all_open();
    }

    fn cancel_all_open(&mut self) {
        let open: Vec<_> = self
            .oms
            .open_orders()
            .filter(|o| o.state != OrderState::PendingCancel)
            .map(|o| o.request.client_id)
            .collect();
        for id in open {
            self.cancel(id);
        }
    }

    fn cancel(&mut self, id: ClientOrderId) {
        if let Err(e) = self.oms.request_cancel(id) {
            self.line(format_args!("cancel-skip {} {e:?}", id.0));
            return;
        }
        match self.venue.cancel(id) {
            Ok(()) => self.line(format_args!("cancel {}", id.0)),
            Err(e) => {
                self.line(format_args!("cancel-failed {} {e:?}", id.0));
                let refused = OrderEvent::CancelRejected {
                    client_id: id,
                    ts: self.clock.now(),
                };
                let _ = self.oms.apply(&refused);
                self.feedback.push_back(refused);
            }
        }
    }

    fn dispatch(&mut self, event: &Event) {
        if self.halted.is_some() {
            return;
        }
        let mut commands = std::mem::take(&mut self.commands);
        self.strategy.on_event(event, &self.book, &mut commands);
        for cmd in commands.drain(..) {
            self.execute(cmd);
        }
        self.commands = commands;
    }

    /// Delivers venue events and local rejections until both queues are empty.
    fn drain(&mut self) {
        let mut budget = MAX_EVENTS_PER_RECORD;
        loop {
            if budget == 0 {
                self.halt(HaltReason::Runaway);
                budget = u32::MAX; // still deliver the cancel acks
            }
            budget -= 1;
            if let Some(event) = self.feedback.pop_front() {
                self.dispatch(&Event::Order(event));
                continue;
            }
            let Some(event) = self.venue.poll() else {
                return;
            };
            let Event::Order(order_event) = event else {
                continue;
            };
            self.line(format_args!("venue {order_event:?}"));
            if let Err(e) = self.oms.apply(&order_event) {
                self.line(format_args!("oms {e:?}"));
                self.halt(HaltReason::IllegalOrderEvent);
                continue;
            }
            if let OrderEvent::Filled {
                client_id,
                price,
                qty,
                ts,
            } = order_event
                && let Some(order) = self.oms.get(client_id)
            {
                let (instrument, side) = (order.request.instrument, order.request.side);
                let notional = i128::from(price.raw()) * i128::from(qty.raw()) / i128::from(SCALE);
                let (dq, dc) = match side {
                    Side::Buy => (qty.raw(), -notional),
                    Side::Sell => (-qty.raw(), notional),
                };
                self.position = Qty::from_raw(self.position.raw() + dq);
                self.cash += dc;
                self.check_daily_loss();
                // #40: a wash-trade pattern is about completed trades, not merely
                // accepted-then-canceled quotes, so this is fed on fill, not on submit.
                self.risk.record_fill(instrument, side, price, qty, ts);
            }
            self.dispatch(&Event::Order(order_event));
        }
    }

    fn context(&self, order: &OrderRequest) -> RiskContext {
        let (mut open_buy, mut open_sell) = (0_i64, 0_i64);
        let (mut own_best_bid, mut own_best_ask) = (None, None);
        for o in self.oms.open_orders() {
            if o.request.instrument == order.instrument {
                match o.request.side {
                    Side::Buy => {
                        open_buy += o.open_qty().raw();
                        own_best_bid = Some(
                            own_best_bid.map_or(o.request.price, |b: Price| b.max(o.request.price)),
                        );
                    }
                    Side::Sell => {
                        open_sell += o.open_qty().raw();
                        own_best_ask = Some(
                            own_best_ask.map_or(o.request.price, |a: Price| a.min(o.request.price)),
                        );
                    }
                }
            }
        }
        RiskContext {
            now: self.clock.now(),
            last_market_data: self.book.last_update(),
            position: self.position,
            open_buy_qty: Qty::from_raw(open_buy),
            open_sell_qty: Qty::from_raw(open_sell),
            // A book awaiting resync keeps levels it knows may be wrong.
            reference_price: self
                .book
                .mid()
                .filter(|_| self.book.status() == BookStatus::Synced),
            daily_pnl: self.daily_pnl(),
            own_best_bid,
            own_best_ask,
        }
    }

    fn reject_locally(&mut self, order: &OrderRequest) {
        self.feedback.push_back(OrderEvent::Rejected {
            client_id: order.client_id,
            ts: self.clock.now(),
        });
    }

    fn execute(&mut self, cmd: Command) {
        match cmd {
            Command::Submit(order) => self.submit(&order),
            Command::Cancel(id) => self.cancel(id),
        }
    }

    fn submit(&mut self, order: &OrderRequest) {
        if self.halted.is_some() {
            self.line(format_args!("drop {} halted", order.client_id.0));
            return;
        }
        if !self.reconciled {
            self.line(format_args!("drop {} unreconciled", order.client_id.0));
            self.reject_locally(order);
            return;
        }
        let ctx = self.context(order);
        if let Err(reject) = self.risk.check(order, &ctx) {
            self.line(format_args!("risk-reject {} {reject:?}", order.client_id.0));
            match reject {
                RiskReject::KillSwitch => self.halt(HaltReason::KillSwitch),
                RiskReject::MaxDailyLoss => self.halt(HaltReason::MaxDailyLoss),
                RiskReject::InvalidOrder
                | RiskReject::StaleData
                | RiskReject::PriceBand
                | RiskReject::SelfCross
                | RiskReject::MaxNotional
                | RiskReject::MaxDailyNotional
                | RiskReject::MaxPosition
                | RiskReject::WashTrade
                | RiskReject::MaxOrderRate => self.reject_locally(order),
            }
            return;
        }
        if let Err(e) = self.oms.insert(*order) {
            self.line(format_args!("oms {e:?}"));
            self.reject_locally(order);
            return;
        }
        match self.venue.submit(order) {
            Ok(()) => {
                self.submitted += 1;
                self.line(format_args!(
                    "submit {} {:?} {:?} {} {}",
                    order.client_id.0, order.side, order.order_type, order.price, order.qty
                ));
            }
            Err(e) => {
                self.line(format_args!("gateway {} {e:?}", order.client_id.0));
                let rejected = OrderEvent::Rejected {
                    client_id: order.client_id,
                    ts: self.clock.now(),
                };
                let _ = self.oms.apply(&rejected);
                self.feedback.push_back(rejected);
            }
        }
    }
}

/// The instrument every committed recording uses.
pub const SAMPLE_INSTRUMENT: InstrumentId = InstrumentId(1);

/// The engine used by `just replay`: the example quoter on a [`SimVenue`].
#[must_use]
pub fn sample_engine(limits: Limits, kill_switch: KillSwitch) -> Engine<InsideQuoter, SimVenue> {
    let qty = Qty::from_raw(SCALE / 1000); // 0.001
    let max_inventory = Qty::from_raw(SCALE / 200); // 0.005
    Engine::new(
        SAMPLE_INSTRUMENT,
        InsideQuoter::new(StrategyId(1), SAMPLE_INSTRUMENT, qty, max_inventory),
        RiskEngine::new(limits, kill_switch),
        SimVenue::new(VenueId(1), SAMPLE_INSTRUMENT),
    )
}

/// Replays `records` through [`sample_engine`], reconciles at the end, and returns the order log.
#[must_use]
pub fn replay(records: &[Record], limits: Limits) -> String {
    let mut engine = sample_engine(limits, KillSwitch::new());
    for r in records {
        engine.venue_mut().on_record(r);
        engine.on_record(r);
    }
    engine.reconcile();
    engine.log
}
