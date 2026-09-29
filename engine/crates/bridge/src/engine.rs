//! The bridge's own engine loop: book + features + signal on one side, risk +
//! OMS + a [`SimVenue`] on the other. Unlike `qc_replay::Engine` there is no
//! [`qc_strategy_runtime::Strategy`]: the external agent decides, and every
//! decision it sends still goes through the same risk path a strategy would.
//!
//! Determinism: time comes only from recorded timestamps via [`SimClock`],
//! and record order is a `Vec`, never a `HashMap`.

use std::collections::BTreeSet;
use std::path::PathBuf;

use ed25519_dalek::SigningKey;
use qc_core::{
    ClientOrderId, Clock, Event, InstrumentId, OrderEvent, OrderRequest, OrderType, Price, Qty,
    SCALE, Side, SimClock, StrategyId, Timestamp, VenueId,
};
use qc_gateway::{Record, SimVenue, VenueAdapter, record::parse as parse_records};
use qc_inference::features::{FeatureState, N_FEATURES, TopOfBook};
use qc_inference::{Direction, Model, Signal};
use qc_oms::{Discrepancy, Oms, OmsError, OrderState, VenueOrder, reconcile as oms_reconcile};
use qc_orderbook::{BookStatus, OrderBook};
use qc_risk::{KillSwitch, Limits, RiskCheck, RiskContext, RiskEngine, RiskReject};
use serde_json::{Map, Value, json};

use crate::approval::{self, CancelFields, OrderFields};
use crate::eventlog;
use crate::instrument::Instrument;

/// The only instrument the v1 bridge trades, and its label on the wire.
pub const INSTRUMENT: InstrumentId = InstrumentId(1);
pub const INSTRUMENT_LABEL: &str = "1";
const BRIDGE_STRATEGY: StrategyId = StrategyId(1);

/// Snake-case wire name for a [`RiskReject`], used by `submit_order_intent`
/// and shared with tests that check exact rejection codes.
#[must_use]
pub fn risk_reject_code(reject: RiskReject) -> &'static str {
    match reject {
        RiskReject::KillSwitch => "kill_switch",
        RiskReject::InvalidOrder => "invalid_order",
        RiskReject::StaleData => "stale_data",
        RiskReject::MaxDailyLoss => "max_daily_loss",
        RiskReject::PriceBand => "price_band",
        RiskReject::SelfCross => "self_cross",
        RiskReject::MaxNotional => "max_notional",
        RiskReject::MaxDailyNotional => "max_daily_notional",
        RiskReject::MaxPosition => "max_position",
        RiskReject::WashTrade => "wash_trade",
        RiskReject::MaxOrderRate => "max_order_rate",
    }
}

/// Human-readable form of a `qc_oms::Discrepancy`, for the `reconcile` op's
/// wire response and the structured event log's `detail` field.
fn describe_discrepancy(d: &Discrepancy) -> String {
    match d {
        Discrepancy::UnknownVenueOrder(v) => format!("unknown_venue_order: venue_order_id={}", v.0),
        Discrepancy::MissingAtVenue(c) => format!("missing_at_venue: client_order_id={}", c.0),
        Discrepancy::OpenAtVenueOnly(c) => format!("open_at_venue_only: client_order_id={}", c.0),
        Discrepancy::FillMismatch {
            client_id,
            local,
            venue,
        } => format!(
            "fill_mismatch: client_order_id={} local={local} venue={venue}",
            client_id.0
        ),
    }
}

/// A loaded model plus the hash the caller asserted for it, echoed on every signal.
pub struct LoadedModel {
    pub model: Box<dyn Model>,
    pub sha256: String,
}

/// Protocol v1.1 `--venue` flag. `Sim` is the default and reproduces v1
/// behaviour exactly (routes every accepted intent to [`SimVenue`]).
/// `External` never routes: an accepted intent is recorded `PendingNew` and
/// returned with a signed [`approval`](crate::approval), and only
/// `report_execution` (from the TS gateway, after it talks to the real
/// venue) may move the order forward.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VenueMode {
    Sim,
    External,
}

impl VenueMode {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Sim => "sim",
            Self::External => "external",
        }
    }
}

impl std::str::FromStr for VenueMode {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "sim" => Ok(Self::Sim),
            "external" => Ok(Self::External),
            other => Err(format!(
                "--venue must be \"sim\" or \"external\", got {other:?}"
            )),
        }
    }
}

/// Why the bridge stopped accepting new orders (protocol v1.2, issue #44).
/// Halting is one-way within the process, mirroring `qc_replay::HaltReason`
/// (`engine/crates/replay/src/lib.rs`) — this crate has its own copy rather
/// than depending on `qc_replay` for one enum, since the two engines are
/// otherwise independent (`qc-bridge` has no `qc_replay::Engine` inside it).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HaltReason {
    KillSwitch,
    MaxDailyLoss,
    /// A `report_execution`/OMS transition the state machine does not allow.
    IllegalOrderEvent,
    /// A `reconcile` op found an unknown venue order, a state or fill
    /// mismatch, or a position mismatch.
    Reconciliation,
}

impl HaltReason {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::KillSwitch => "kill_switch",
            Self::MaxDailyLoss => "max_daily_loss",
            Self::IllegalOrderEvent => "illegal_order_event",
            Self::Reconciliation => "reconciliation",
        }
    }
}

/// Snake-case wire name for an [`OrderState`], used by `status()` so a
/// caller (and this crate's own tests) can tell a pending-external-approval
/// order apart from one already resting at the venue.
#[must_use]
const fn order_state_str(state: OrderState) -> &'static str {
    match state {
        OrderState::PendingNew => "pending_new",
        OrderState::New => "new",
        OrderState::PartiallyFilled => "partially_filled",
        OrderState::Filled => "filled",
        OrderState::PendingCancel => "pending_cancel",
        OrderState::Canceled => "canceled",
        OrderState::Rejected => "rejected",
    }
}

pub struct BridgeEngine {
    book: OrderBook,
    tick: Price,
    features: FeatureState,
    last_features: Option<[f64; N_FEATURES]>,
    model: Option<LoadedModel>,
    risk: RiskEngine,
    oms: Oms,
    venue: SimVenue,
    clock: SimClock,
    position: Qty,
    /// Cash from fills, raw [`Price`] units, wide so it cannot overflow.
    cash: i128,
    /// One-way once set (root rule: a human restarts the process), mirroring
    /// `qc_replay::Engine::halted`.
    halted: Option<HaltReason>,
    /// `external` mode only: false until a `reconcile` op finds no
    /// discrepancy; `sim` mode has no venue to reconcile with and starts
    /// (and stays) `true`, unchanged from v1.1 behaviour.
    reconciled: bool,
    records: Vec<Record>,
    cursor: usize,
    decide_every: u32,
    since_last_decision: u32,
    decision_seq: u64,
    next_client_id: u64,
    venue_mode: VenueMode,
    /// Fresh per process (protocol v1.1): never persisted, never reused
    /// across a restart, so an old approval cannot outlive the process that
    /// minted it.
    signing_key: SigningKey,
    /// `--kill-file` (protocol v1.2, issue #44): polled on every record/op;
    /// its existence engages the kill switch independent of any op the
    /// agent sends, so a stuck or crashed agent process cannot block it.
    kill_file: Option<PathBuf>,
    /// `--instrument` (protocol v1.2, issue #66): `None` keeps the v1
    /// hard-coded instrument and skips every tick/qty-step/trading-hours check.
    instrument_cfg: Option<Instrument>,
    /// Client-order-ids whose `PendingCancel` was requested by
    /// [`Self::cancel_all_open`] (a halt), not an explicit
    /// `cancel_order_intent` call (protocol v1.2.1, issue #44's cancel-on-halt
    /// gap): tags the approval [`Self::pending_cancels`] mints with
    /// `"reason":"halt"` so ops/audit can tell a mass halt-cancel apart from a
    /// one-off agent-requested cancel. A `BTreeSet`, not a `HashMap` (root
    /// rule), bounded by however many orders were ever open at once.
    halt_cancelled: BTreeSet<u64>,
}

impl BridgeEngine {
    #[must_use]
    pub fn new(
        records: Vec<Record>,
        limits: Limits,
        kill_switch: KillSwitch,
        tick: Price,
        decide_every: u32,
        model: Option<LoadedModel>,
        venue_mode: VenueMode,
    ) -> Self {
        Self::with_instrument(
            records,
            limits,
            kill_switch,
            tick,
            decide_every,
            model,
            venue_mode,
            None,
            None,
        )
    }

    /// Same as [`Self::new`] plus protocol v1.2's `--kill-file` (issue #44)
    /// and `--instrument` (issue #66), both `None` by default so every
    /// existing call site (and `just replay`'s sibling `just agent-sim`,
    /// which never sets either flag) keeps today's behaviour byte-for-byte.
    #[must_use]
    #[allow(clippy::too_many_arguments)]
    pub fn with_instrument(
        records: Vec<Record>,
        limits: Limits,
        kill_switch: KillSwitch,
        tick: Price,
        decide_every: u32,
        model: Option<LoadedModel>,
        venue_mode: VenueMode,
        kill_file: Option<PathBuf>,
        instrument_cfg: Option<Instrument>,
    ) -> Self {
        let instrument_id = instrument_cfg.as_ref().map_or(INSTRUMENT, |i| i.id);
        Self {
            book: OrderBook::new(instrument_id),
            tick,
            features: FeatureState::new(tick),
            last_features: None,
            model,
            risk: RiskEngine::new(limits, kill_switch),
            oms: Oms::default(),
            venue: SimVenue::new(VenueId(1), instrument_id),
            clock: SimClock::default(),
            position: Qty::ZERO,
            cash: 0,
            halted: None,
            reconciled: venue_mode == VenueMode::Sim,
            records,
            cursor: 0,
            decide_every,
            since_last_decision: 0,
            decision_seq: 0,
            next_client_id: 0,
            venue_mode,
            signing_key: approval::generate_signing_key(),
            kill_file,
            instrument_cfg,
            halt_cancelled: BTreeSet::new(),
        }
    }

    fn instrument_id(&self) -> InstrumentId {
        self.instrument_cfg.as_ref().map_or(INSTRUMENT, |i| i.id)
    }

    /// Wire label `DecisionRequest.instrument`/`OrderIntent.instrument` must
    /// match: the configured symbol (issue #66) or the v1 placeholder `"1"`.
    #[must_use]
    pub fn instrument_label(&self) -> &str {
        self.instrument_cfg
            .as_ref()
            .map_or(INSTRUMENT_LABEL, |i| i.symbol.as_str())
    }

    #[must_use]
    pub const fn venue_mode(&self) -> VenueMode {
        self.venue_mode
    }

    /// Sets `--kill-file` after construction (mainly for tests that don't
    /// otherwise need the fuller [`Self::with_instrument`] constructor).
    pub fn set_kill_file(&mut self, path: PathBuf) {
        self.kill_file = Some(path);
    }

    /// `hello` response body (protocol v1.2): the protocol version this
    /// binary speaks, the venue mode it was started with, the public key
    /// every approval it mints can be verified against, and `market_ts_ns`
    /// (the latest market/feed timestamp this process has seen) — the
    /// gateway's approval-expiry clock (issue #45's gap: it must never
    /// compare against `Date.now()`).
    #[must_use]
    pub fn hello(&self) -> Value {
        json!({
            "protocol": "1.2",
            "venue_mode": self.venue_mode.as_str(),
            "approval_public_key": approval::encode_public_key(&self.signing_key.verifying_key()),
            "market_ts_ns": self.clock.now().0,
        })
    }

    /// Parses a recording in the `tests/replay` text format (shared with `qc_replay`).
    ///
    /// # Errors
    /// [`qc_gateway::record::ParseError`] on the first malformed line.
    pub fn load_recording(text: &str) -> Result<Vec<Record>, qc_gateway::record::ParseError> {
        parse_records(text)
    }

    /// Appends newly available records to the feed (protocol v1.2, `--follow`,
    /// issue #48/shadow lane): lets `main.rs`'s tailing reader (`feed.rs`)
    /// grow the feed as a recording file grows, instead of requiring the
    /// whole file up front. Purely additive to [`Self::next_decision_request`]'s
    /// existing `cursor < self.records.len()` loop — record order and the
    /// engine's clock/decision logic are unchanged, so "same final file ->
    /// same decisions" continues to hold regardless of when each append
    /// happened.
    pub fn feed_records(&mut self, more: impl IntoIterator<Item = Record>) {
        self.records.extend(more);
    }

    fn daily_pnl(&self) -> Price {
        let mark = self.book.mid().map_or(0, |m| i128::from(m.raw()));
        let pnl = self.cash + i128::from(self.position.raw()) * mark / i128::from(SCALE);
        Price::from_raw(i64::try_from(pnl).unwrap_or(if pnl < 0 { i64::MIN } else { i64::MAX }))
    }

    fn check_daily_loss(&mut self) {
        if self.halted.is_none() && self.risk.daily_loss_reached(self.daily_pnl()) {
            self.halt(HaltReason::MaxDailyLoss, None);
        }
    }

    /// Detects an externally-engaged [`KillSwitch`] (another thread holding
    /// a clone, root rule 11) — the bridge itself never engages this flag
    /// for any reason but a genuine kill (the `kill` op or `--kill-file`;
    /// see [`Self::kill`]/[`Self::poll_kill_file`]).
    pub(crate) fn check_kill_switch(&mut self) {
        if self.halted.is_none() && self.risk.kill_switch().is_engaged() {
            self.halt(HaltReason::KillSwitch, None);
        }
    }

    /// `--kill-file` (protocol v1.2, issue #44): an out-of-band trigger
    /// independent of stdin, so a stuck or crashed agent process cannot
    /// block it — anything that can still touch the filesystem (an
    /// operator, a supervisor, the ops runbook) engages the switch, and the
    /// *next* record or op this process handles (from any source, not
    /// necessarily the agent) observes it within root rule 11's budget.
    ///
    /// Issue #44 reopened: this alone is not enough if *nothing* ever calls
    /// the bridge again (a truly hung, not merely slow, agent process never
    /// sends another op, and a stalled feed never produces another record).
    /// `main.rs` now also runs a dedicated watcher thread that polls this
    /// same path independent of stdin and engages the same `Arc<AtomicBool>`
    /// kill switch directly; the `!is_engaged()` guard below is what makes
    /// the two paths idempotent with each other (whichever detects the file
    /// first logs the one `kill_switch_engaged` event, the other is a silent
    /// no-op) rather than double-logging.
    pub fn poll_kill_file(&mut self) {
        if self.halted.is_some() {
            return;
        }
        let Some(path) = &self.kill_file else { return };
        if path.exists() {
            if !self.risk.kill_switch().is_engaged() {
                self.risk.kill_switch().engage();
                eventlog::kill_switch_engaged(self.clock.now().0, "file");
            }
            self.halt(HaltReason::KillSwitch, None);
        }
    }

    /// `kill` op (protocol v1.2, issue #44): the same path as an
    /// externally-engaged switch or `--kill-file`, triggered in-band instead.
    pub fn kill(&mut self, reason: &str) {
        if self.halted.is_none() {
            self.risk.kill_switch().engage();
            eventlog::kill_switch_engaged(self.clock.now().0, "op");
        }
        self.halt(HaltReason::KillSwitch, Some(reason));
    }

    /// Stops all order entry for good (one-way; a human restarts the
    /// process) and cancels every working order, mirroring
    /// `qc_replay::Engine::halt`. Idempotent: the first reason recorded is
    /// the one that sticks, and only the first call logs and cancels.
    fn halt(&mut self, reason: HaltReason, detail: Option<&str>) {
        if self.halted.is_some() {
            return;
        }
        self.halted = Some(reason);
        eventlog::halt(self.clock.now().0, reason.as_str(), detail);
        self.cancel_all_open();
    }

    /// Requests a cancel for every non-terminal order (risk never blocks a
    /// cancel). `sim` mode also routes the cancel to `SimVenue` and drains
    /// its ack, exactly as `qc_replay::Engine::cancel_all_open` does; there
    /// is no venue to route to in `external` mode (no code path ever
    /// submitted to `SimVenue` there), so the OMS moves to `PendingCancel`
    /// and the TS gateway — which does hold the real venue connection —
    /// observes that through `status` and finishes the cancel for real
    /// (issue #44's acceptance criteria: verified in this crate's own suite
    /// via `SimVenue` in CI, and in the broker-adapter's own suite against a
    /// recorded venue response).
    fn cancel_all_open(&mut self) {
        let open: Vec<_> = self
            .oms
            .open_orders()
            .filter(|o| o.state != OrderState::PendingCancel)
            .map(|o| o.request.client_id)
            .collect();
        for id in open {
            if self.oms.request_cancel(id).is_ok() && self.venue_mode == VenueMode::External {
                // Issue #44's cancel-on-halt gap: tag this id so
                // `Self::pending_cancels` marks the approval it mints
                // `"reason":"halt"`, distinguishing it from an explicit
                // `cancel_order_intent` call.
                self.halt_cancelled.insert(id.0);
            }
            if self.venue_mode == VenueMode::Sim {
                let _ = self.venue.cancel(id);
            }
        }
        if self.venue_mode == VenueMode::Sim {
            self.drain_venue_events();
        }
    }

    /// Protocol v1.2.1 (issue #44's cancel-on-halt gap): a fresh, unexpired
    /// cancel `approval` for every order currently `PendingCancel` —
    /// regardless of whether it got there via a halt
    /// ([`Self::cancel_all_open`]) or an explicit `cancel_order_intent` —
    /// tagged `"reason":"halt"` for the former (see `halt_cancelled`).
    /// Minted fresh on every call rather than stored once: the gateway's
    /// heartbeat (agent/src/broker/heartbeat.ts) is expected to call this
    /// repeatedly until nothing is open at the broker, and a freshly minted
    /// approval is never stale by the time it acts on it, even if the
    /// bridge's market clock has stopped advancing (halted, `external` mode:
    /// no more decisions, so `now` and `expires_ts_ns` are computed from the
    /// same frozen reading and the approval never appears expired).
    /// `sim` mode never leaves an order `PendingCancel` (`SimVenue` acks a
    /// cancel immediately), so this is always empty there.
    #[must_use]
    pub fn pending_cancels(&self) -> Vec<Value> {
        self.oms
            .open_orders()
            .filter(|o| o.state == OrderState::PendingCancel)
            .map(|o| {
                let client_order_id = o.request.client_id.0;
                let reason = self
                    .halt_cancelled
                    .contains(&client_order_id)
                    .then_some("halt");
                let fields = CancelFields {
                    client_order_id,
                    reason,
                };
                let approval =
                    approval::mint_cancel(&self.signing_key, &fields, self.clock.now().0);
                json!({"client_order_id": client_order_id, "approval": approval})
            })
            .collect()
    }

    /// Remaining loss budget for the day: the limit, less any loss already
    /// booked. Profit never raises it past the configured limit.
    fn remaining_daily_loss(&self) -> Price {
        let max = i128::from(self.risk.limits().max_daily_loss.raw());
        let pnl = i128::from(self.daily_pnl().raw());
        let headroom = (max + pnl.min(0)).max(0);
        Price::from_raw(i64::try_from(headroom).unwrap_or(i64::MAX))
    }

    fn context(&self) -> RiskContext {
        let (mut open_buy, mut open_sell) = (0_i64, 0_i64);
        let (mut own_best_bid, mut own_best_ask) = (None, None);
        for o in self.oms.open_orders() {
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
        RiskContext {
            now: self.clock.now(),
            last_market_data: self.book.last_update(),
            position: self.position,
            open_buy_qty: Qty::from_raw(open_buy),
            open_sell_qty: Qty::from_raw(open_sell),
            reference_price: self
                .book
                .mid()
                .filter(|_| self.book.status() == BookStatus::Synced),
            daily_pnl: self.daily_pnl(),
            own_best_bid,
            own_best_ask,
        }
    }

    /// Applies one order-lifecycle event to the OMS, updating position and
    /// cash on a fill. Shared by [`Self::drain_venue_events`] (`sim` mode,
    /// events from `SimVenue`) and [`Self::report_execution`] (`external`
    /// mode, events reported by the TS gateway after it talks to the real
    /// venue) so the fill bookkeeping has exactly one implementation.
    fn apply_order_event(&mut self, event: &OrderEvent) -> Result<OrderState, OmsError> {
        let state = self.oms.apply(event)?;
        if let OrderEvent::Filled {
            client_id,
            price,
            qty,
            ts,
        } = *event
            && let Some(order) = self.oms.get(client_id)
        {
            let notional = i128::from(price.raw()) * i128::from(qty.raw()) / i128::from(SCALE);
            let (dq, dc) = match order.request.side {
                Side::Buy => (qty.raw(), -notional),
                Side::Sell => (-qty.raw(), notional),
            };
            self.position = Qty::from_raw(self.position.raw() + dq);
            self.cash += dc;
            // #40: a wash-trade pattern is about completed trades, not merely
            // accepted-then-canceled quotes, so it is fed on fill (sim or
            // gateway-reported), not on submit.
            self.risk
                .record_fill(order.request.instrument, order.request.side, price, qty, ts);
        }
        Ok(state)
    }

    /// Applies venue events (fills, acks) to the OMS. Mirrors
    /// `qc_replay::Engine::drain`'s fill bookkeeping.
    fn drain_venue_events(&mut self) {
        while let Some(event) = self.venue.poll() {
            let Event::Order(order_event) = event else {
                continue;
            };
            // This process is the only client of this venue, so an illegal
            // event should never happen; skip it defensively rather than
            // letting a stdio bridge process panic.
            let _ = self.apply_order_event(&order_event);
        }
        self.check_daily_loss();
    }

    /// `report_execution` (protocol v1.1, gateway -> bridge, `external`
    /// mode): applies a real venue's report of what happened to one order to
    /// the OMS. A legal transition applies exactly as a `SimVenue` event
    /// would; an illegal one (a report that cannot follow from the order's
    /// current state — the reconciliation failure root rule 12 asks the
    /// bridge to fail closed on) is never applied and instead halts the
    /// engine, since our view of position/exposure would otherwise be wrong
    /// and nothing downstream can trust it.
    ///
    /// # Errors
    /// The [`OmsError`] that made the transition illegal.
    pub fn report_execution(&mut self, event: OrderEvent, ts_ns: u64) -> Result<(), OmsError> {
        self.clock.advance_to(Timestamp(ts_ns));
        let result = self.apply_order_event(&event);
        self.check_daily_loss();
        if let Err(e) = &result {
            self.halt(HaltReason::IllegalOrderEvent, Some(&format!("{e:?}")));
        }
        result.map(|_| ())
    }

    /// `cancel_order_intent` (protocol v1.2, issue #66's gap list): always
    /// allowed for one of our own non-terminal orders — risk never blocks a
    /// cancel, and this runs even while halted (halting itself already
    /// requests every cancel via [`Self::cancel_all_open`], so this is
    /// idempotent with that path). An id this process never opened is an
    /// error, not a silently-ignored no-op.
    ///
    /// `sim` mode routes the cancel to `SimVenue` immediately, as
    /// `submit_order_intent` already does for a new order. `external` mode
    /// mints a signed `approval` over `{"action":"cancel","client_order_id",
    /// "expires_ts_ns"}` (same key, same 5 s TTL) instead, exactly as
    /// `submit_order_intent` does for a new order's approval.
    ///
    /// # Errors
    /// A `&'static str` reason the cancel could not even be attempted:
    /// `client_order_id` is unknown, the order is already terminal, or the
    /// OMS's own state machine does not allow a cancel request from its
    /// current state.
    pub fn cancel_order_intent(
        &mut self,
        client_order_id: u64,
        _reason: &str,
    ) -> Result<Value, &'static str> {
        let id = ClientOrderId(client_order_id);
        let Some(order) = self.oms.get(id) else {
            return Err("unknown client_order_id");
        };
        if order.state.is_terminal() {
            return Err("order is already terminal");
        }
        self.oms
            .request_cancel(id)
            .map_err(|_| "cancel is not legal from this order's state")?;
        if self.venue_mode == VenueMode::External {
            let fields = CancelFields {
                client_order_id,
                reason: None,
            };
            let approval = approval::mint_cancel(&self.signing_key, &fields, self.clock.now().0);
            return Ok(
                json!({"accepted": true, "client_order_id": client_order_id, "approval": approval}),
            );
        }
        let _ = self.venue.cancel(id);
        self.drain_venue_events();
        Ok(json!({"accepted": true, "client_order_id": client_order_id}))
    }

    /// `reconcile` (protocol v1.2, issue #45's gap): compares the gateway's
    /// view of venue orders and position with the OMS. Any discrepancy
    /// halts (reason `"reconciliation"`) and returns the diff; a clean
    /// compare marks the bridge reconciled (`external` mode refuses new
    /// intents until the first one, like `qc_replay::Engine`). Cash is
    /// accepted but not compared: this process has no independent ground
    /// truth for fees/dividends/interest the way it does for order state and
    /// position, so a mismatch there is not (yet) treated as a discrepancy.
    pub fn reconcile_with_venue(&mut self, venue_orders: &[VenueOrder], position: Qty) -> Value {
        let r = oms_reconcile(&self.oms, venue_orders);
        let mut discrepancies: Vec<String> =
            r.discrepancies.iter().map(describe_discrepancy).collect();
        if self.position != position {
            discrepancies.push(format!(
                "position_mismatch: local={} venue={}",
                self.position, position
            ));
        }
        let ok = discrepancies.is_empty();
        let detail = (!ok).then(|| discrepancies.join("; "));
        eventlog::reconcile(self.clock.now().0, ok, detail.as_deref());
        if ok {
            self.reconciled = true;
        } else {
            self.halt(HaltReason::Reconciliation, detail.as_deref());
        }
        json!({"ok": ok, "discrepancies": discrepancies})
    }

    /// Checks `qty`/`limit_price` against the configured instrument's tick
    /// size, quantity step and trading hours (issue #66). `None` when no
    /// `--instrument` is configured (today's default: no such check exists).
    fn instrument_reject(&self, qty: Qty, limit_price: Price) -> Option<&'static str> {
        let cfg = self.instrument_cfg.as_ref()?;
        if !cfg.trading_hours.is_open(self.clock.now().0) {
            return Some("outside_trading_hours");
        }
        if !cfg.qty_is_valid(qty) {
            return Some("invalid_qty_step");
        }
        if !cfg.price_is_valid(limit_price) {
            return Some("invalid_tick_size");
        }
        None
    }

    fn log_accepted(
        &self,
        client_order_id: u64,
        instrument: &str,
        side: &str,
        qty: Qty,
        price: Price,
    ) {
        eventlog::accepted_intent(
            self.clock.now().0,
            client_order_id,
            instrument,
            side,
            &qty.to_string(),
            &price.to_string(),
        );
    }

    fn update_features_and_signal(&mut self) {
        let synced = self.book.status() == BookStatus::Synced;
        let sides = synced
            .then(|| self.book.best_bid().zip(self.book.best_ask()))
            .flatten();
        let Some(((bid_px, bid_qty), (ask_px, ask_qty))) = sides else {
            // Out of sync or one-sided: restart the warmup window rather than
            // bridging a feature update across a gap the book itself resyncs from.
            self.features = FeatureState::new(self.tick);
            self.last_features = None;
            return;
        };
        let tob = TopOfBook {
            bid_px,
            bid_qty,
            ask_px,
            ask_qty,
        };
        self.last_features = self.features.update(tob);
    }

    fn process_record(&mut self, record: &Record) {
        self.clock.advance_to(record.ts_local());
        self.poll_kill_file();
        self.check_kill_switch();
        self.venue.on_record(record);
        match record {
            Record::Snapshot(s) => {
                let _ = self.book.apply_snapshot(s);
                self.since_last_decision += 1;
            }
            Record::Market(qc_core::MarketEvent::BookDelta(d)) => {
                let _ = self.book.apply(d);
                self.since_last_decision += 1;
            }
            Record::Market(qc_core::MarketEvent::Trade(_)) => {}
        }
        self.drain_venue_events();
        self.update_features_and_signal();
    }

    fn ready_to_decide(&self) -> bool {
        self.book.status() == BookStatus::Synced
            && self.book.best_bid().is_some()
            && self.book.best_ask().is_some()
            && self.since_last_decision >= self.decide_every
    }

    fn signal_json(&self) -> Option<Value> {
        let model = self.model.as_ref()?;
        let features = self.last_features?;
        let signal: Signal = model.model.predict(&features)?;
        let direction = match signal.direction {
            Direction::Up => "up",
            Direction::Flat => "flat",
            Direction::Down => "down",
        };
        Some(json!({
            "direction": direction,
            "probs": signal.probs,
            "model_sha256": model.sha256,
        }))
    }

    fn features_json(&self) -> Value {
        let map: Map<String, Value> = self.last_features.map_or_else(Map::new, |f| {
            qc_inference::features::FEATURE_NAMES
                .iter()
                .zip(f)
                .map(|(name, v)| ((*name).to_owned(), json!(v)))
                .collect()
        });
        Value::Object(map)
    }

    fn build_decision_request(&mut self) -> Value {
        self.decision_seq += 1;
        let (bid_px, _) = self.book.best_bid().expect("caller checked readiness");
        let (ask_px, _) = self.book.best_ask().expect("caller checked readiness");
        let mid = self.book.mid().expect("both sides present when ready");
        let spread_ticks = if self.tick.raw() > 0 {
            ((ask_px.raw() - bid_px.raw()) / self.tick.raw()).max(0)
        } else {
            0
        };
        let limits = self.risk.limits();
        let now_ns = self.clock.now().0;
        // #66: the engine, not the agent, decides what is even offerable —
        // outside trading hours (when an instrument is configured) buy/sell
        // are not offered at all, not just rejected if attempted.
        let trading = self
            .instrument_cfg
            .as_ref()
            .is_none_or(|i| i.trading_hours.is_open(now_ns));
        let allowed_actions: Vec<&str> = if trading {
            vec!["buy", "sell", "no_trade"]
        } else {
            vec!["no_trade"]
        };
        json!({
            "request_id": format!("dr-{}", self.decision_seq),
            "ts_ns": now_ns,
            "instrument": self.instrument_label(),
            "best_bid": bid_px.to_string(),
            "best_ask": ask_px.to_string(),
            "mid": mid.to_string(),
            "spread_ticks": spread_ticks,
            "features": self.features_json(),
            "signal": self.signal_json(),
            "position": self.position.to_string(),
            "limits": {
                "max_position": limits.max_position.to_string(),
                "max_notional": limits.max_notional.to_string(),
                "max_order_rate_per_sec": limits.max_order_rate_per_sec,
                "remaining_daily_loss": self.remaining_daily_loss().to_string(),
            },
            "allowed_actions": allowed_actions,
        })
    }

    /// Advances the feed until the book is ready for a decision, or the
    /// recording is exhausted (`None`).
    pub fn next_decision_request(&mut self) -> Option<Value> {
        while self.cursor < self.records.len() {
            let record = self.records[self.cursor].clone();
            self.cursor += 1;
            self.process_record(&record);
            if self.ready_to_decide() {
                self.since_last_decision = 0;
                return Some(self.build_decision_request());
            }
        }
        None
    }

    /// Runs the same risk path `qc_replay::Engine` runs before every order,
    /// then records to the OMS. Never routes an order that failed a check.
    ///
    /// In `sim` mode (default, v1-compatible) an accepted order routes to
    /// [`SimVenue`] exactly as before. In `external` mode it is never
    /// routed: it is recorded `PendingNew` and the result carries a signed
    /// `approval` (protocol v1.1, issue #34) instead, so the TS gateway can
    /// act on this decision at a real venue without re-checking it, and
    /// report what happened back through `report_execution`.
    pub fn submit_order_intent(
        &mut self,
        request_id: &str,
        side: Side,
        qty: Qty,
        limit_price: Price,
        time_in_force: OrderType,
    ) -> Value {
        self.poll_kill_file();
        self.check_kill_switch();
        if let Some(reason) = self.halted {
            return json!({"accepted": false, "halted": reason.as_str()});
        }
        if self.venue_mode == VenueMode::External && !self.reconciled {
            // #45: external mode refuses new intents until the first clean
            // reconcile, like `qc_replay::Engine`'s own `unreconciled` gate.
            return json!({"accepted": false, "risk_reject": "unreconciled"});
        }
        let instrument_label = self.instrument_label().to_owned();
        if let Some(code) = self.instrument_reject(qty, limit_price) {
            eventlog::risk_reject(self.clock.now().0, code, &instrument_label);
            return json!({"accepted": false, "risk_reject": code});
        }
        self.next_client_id += 1;
        let client_id = ClientOrderId(self.next_client_id);
        let order = OrderRequest {
            client_id,
            strategy: BRIDGE_STRATEGY,
            instrument: self.instrument_id(),
            side,
            order_type: time_in_force,
            price: limit_price,
            qty,
        };
        let ctx = self.context();
        if let Err(reject) = self.risk.check(&order, &ctx) {
            eventlog::risk_reject(
                self.clock.now().0,
                risk_reject_code(reject),
                &instrument_label,
            );
            return match reject {
                RiskReject::KillSwitch => {
                    self.halt(HaltReason::KillSwitch, None);
                    json!({"accepted": false, "halted": "kill_switch"})
                }
                RiskReject::MaxDailyLoss => {
                    self.halt(HaltReason::MaxDailyLoss, None);
                    json!({"accepted": false, "halted": "max_daily_loss"})
                }
                other => json!({"accepted": false, "risk_reject": risk_reject_code(other)}),
            };
        }
        if self.oms.insert(order).is_err() {
            return json!({"accepted": false, "risk_reject": "invalid_order"});
        }
        let side_str = match side {
            Side::Buy => "buy",
            Side::Sell => "sell",
        };
        if self.venue_mode == VenueMode::External {
            let fields = OrderFields {
                client_order_id: client_id.0,
                request_id,
                instrument: &instrument_label,
                side: side_str,
                qty,
                limit_price,
                // Mirrors `wire::WireTimeInForce`'s mapping (`Gtc` is the
                // only source of `OrderType::Limit` on this wire path):
                // reconstructs the wire spelling the agent originally sent.
                time_in_force: match time_in_force {
                    OrderType::Ioc => "ioc",
                    OrderType::Limit | OrderType::PostOnly => "gtc",
                },
            };
            let approval = approval::mint(&self.signing_key, &fields, self.clock.now().0);
            self.log_accepted(client_id.0, &instrument_label, side_str, qty, limit_price);
            return json!({
                "accepted": true,
                "client_order_id": client_id.0,
                "approval": approval,
            });
        }
        if self.venue.submit(&order).is_ok() {
            self.drain_venue_events();
            self.log_accepted(client_id.0, &instrument_label, side_str, qty, limit_price);
            return json!({"accepted": true, "client_order_id": client_id.0});
        }
        let rejected = OrderEvent::Rejected {
            client_id,
            ts: self.clock.now(),
        };
        let _ = self.oms.apply(&rejected);
        json!({"accepted": false, "risk_reject": "invalid_order"})
    }

    #[must_use]
    pub fn status(&self) -> Value {
        let open_orders: Vec<Value> = self
            .oms
            .open_orders()
            .map(|o| {
                json!({
                    "client_order_id": o.request.client_id.0,
                    "side": match o.request.side { Side::Buy => "buy", Side::Sell => "sell" },
                    "price": o.request.price.to_string(),
                    "qty": o.request.qty.to_string(),
                    "filled": o.filled.to_string(),
                    "state": order_state_str(o.state),
                })
            })
            .collect();
        let limits = self.risk.limits();
        json!({
            "position": self.position.to_string(),
            "halted": self.halted.map(HaltReason::as_str),
            "reconciled": self.reconciled,
            "market_ts_ns": self.clock.now().0,
            "open_orders": open_orders,
            // Issue #44's cancel-on-halt gap (protocol v1.2.1): every currently
            // `PendingCancel` order's fresh cancel approval, so the gateway's
            // heartbeat can finish the cancel at the real broker even if it
            // missed (or is polling less often than) `drain_halt_cancels`.
            "pending_cancels": self.pending_cancels(),
            "limits": {
                "max_position": limits.max_position.to_string(),
                "max_notional": limits.max_notional.to_string(),
                "max_order_rate_per_sec": limits.max_order_rate_per_sec,
                "remaining_daily_loss": self.remaining_daily_loss().to_string(),
            },
        })
    }
}
