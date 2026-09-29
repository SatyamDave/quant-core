//! Pre-trade risk. Protected zone: changes need human approval, and no change
//! may loosen a limit or add a bypass. This crate has no cargo features on
//! purpose: there is no build in which the checks are off.
//!
//! Every check and its boundary, in the order [`RiskEngine::check`] runs them:
//! 1. kill switch engaged: reject. Checked before anything else.
//! 2. price or quantity not positive: reject.
//! 3. stale data: age `now - last_market_data` equal to `stale_data_ms` passes, one ns more fails.
//! 4. daily loss: `daily_pnl <= -max_daily_loss` fails, so reaching the limit halts; one unit less loss passes.
//! 5. price band: `|price - reference| * 10_000 <= price_band_bps * reference` passes; no reference fails.
//! 6. self-cross (#40): a buy at or above our own resting ask, or a sell at or below our
//!    own resting bid, would fill against our own order; rejected regardless of what the
//!    caller claims its intent was. One tick less aggressive than the resting price passes.
//! 7. notional: `price * qty <= max_notional` passes; overflow fails.
//! 8. daily notional (#41): cumulative notional of orders accepted so far this exchange day,
//!    plus this order's notional, `<= max_daily_notional` passes. The counter resets when
//!    `now` falls in a later UTC day than the last accepted order (a placeholder for the
//!    real exchange-calendar decision, pending #23).
//! 9. position: worst-case position if every open order on the order's side and
//!    this order fill, `|position + open same-side + qty| <= max_position` passes.
//! 10. wash-trade pattern (#40): a completed fill of ours, of the opposite side, same
//!     price and same quantity (so net position change is zero), within
//!     `wash_trade_window_ms` of `now` is a wash-trade pattern; rejected regardless of
//!     stated intent. Evaluated against fills the caller reports through
//!     [`RiskEngine::record_fill`], not merely-accepted orders: an order that never
//!     filled never traded, so a two-sided market-making quote (a resting bid and ask,
//!     one later canceled unfilled) is not by itself a wash trade.
//! 11. order rate: at most `max_order_rate_per_sec` passed orders in any window
//!     `(now - 1 s, now]`; the order that would be one more fails.
//!
//! Only an order that passes every check counts toward the rate window and the daily
//! notional counter; the wash-trade history is populated separately, only by
//! [`RiskEngine::record_fill`].

use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use qc_core::{InstrumentId, OrderRequest, Price, Qty, Side, Timestamp};
use serde::Deserialize;

/// Per-strategy limits, loaded from `config/limits/*.toml`. Unknown keys are an
/// error so a misspelled limit cannot be silently ignored.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Limits {
    /// Largest absolute position per instrument, in base units.
    pub max_position: Qty,
    /// Largest notional of a single order, in quote currency.
    pub max_notional: Price,
    pub max_order_rate_per_sec: u32,
    /// Realized plus unrealized loss for the day that halts trading, as a positive amount.
    pub max_daily_loss: Price,
    /// Largest distance of an order price from the reference price, in basis points.
    pub price_band_bps: u32,
    /// Market data older than this halts order entry.
    pub stale_data_ms: u64,
    /// Cumulative notional of accepted orders per exchange day, in quote currency (#41).
    /// Many broker accounts carry no platform-side spending cap, so this may be the
    /// only enforcement; set it to the operator's agreed number.
    pub max_daily_notional: Price,
    /// Window in which an accepted, opposite-side, same-price, same-quantity order is a
    /// wash-trade pattern (#40).
    pub wash_trade_window_ms: u64,
}

/// What a check needs to know about the world when an order is proposed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RiskContext {
    /// From the engine's [`qc_core::Clock`], so replay drives the rate window deterministically.
    pub now: Timestamp,
    pub last_market_data: Timestamp,
    /// Signed position in the order's instrument.
    pub position: Qty,
    /// Unfilled quantity of our working buy orders in the instrument.
    pub open_buy_qty: Qty,
    /// Unfilled quantity of our working sell orders in the instrument (positive).
    pub open_sell_qty: Qty,
    /// Reference price for the fat-finger band, usually the mid. `None` rejects.
    pub reference_price: Option<Price>,
    pub daily_pnl: Price,
    /// Highest price among our own open buy orders in this instrument, for self-cross
    /// detection (#40). `None` when we have no resting buy.
    pub own_best_bid: Option<Price>,
    /// Lowest price among our own open sell orders in this instrument, for self-cross
    /// detection (#40). `None` when we have no resting sell.
    pub own_best_ask: Option<Price>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RiskReject {
    KillSwitch,
    InvalidOrder,
    StaleData,
    MaxDailyLoss,
    PriceBand,
    /// Would fill against our own resting order in this instrument (#40).
    SelfCross,
    MaxNotional,
    /// Cumulative notional accepted so far this exchange day would exceed the cap (#41).
    MaxDailyNotional,
    MaxPosition,
    /// An accepted, opposite-side, same-price, same-quantity order landed inside the
    /// wash-trade detection window (#40).
    WashTrade,
    MaxOrderRate,
}

/// Global kill switch. Clones share one flag, so any thread (an operator
/// command, a watchdog) can halt every engine that holds a clone. There is no
/// reset: clearing it means restarting the process, a human decision.
#[derive(Debug, Clone, Default)]
pub struct KillSwitch(Arc<AtomicBool>);

impl KillSwitch {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn engage(&self) {
        self.0.store(true, Ordering::SeqCst);
    }

    #[must_use]
    pub fn is_engaged(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }
}

/// Runs before every order. There is no way to skip it.
pub trait RiskCheck {
    /// Checks the order and, if it passes, counts it toward the rate limit.
    ///
    /// # Errors
    /// Returns the first [`RiskReject`] the order violates.
    fn check(&mut self, order: &OrderRequest, ctx: &RiskContext) -> Result<(), RiskReject>;
}

const NANOS_PER_MS: u64 = 1_000_000;
const NANOS_PER_SEC: u64 = 1_000_000_000;
/// A day, used only to bucket [`Limits::max_daily_notional`] (#41). A placeholder for the
/// real exchange-calendar decision (pending #23): deterministic and replay-safe, but not
/// necessarily the trading venue's own session boundary.
const NANOS_PER_DAY: u64 = 24 * 60 * 60 * NANOS_PER_SEC;

#[derive(Debug)]
pub struct RiskEngine {
    limits: Limits,
    kill_switch: KillSwitch,
    /// Times of passed orders inside the current one-second window, oldest first.
    /// Capacity is the rate limit, reserved up front so a check never allocates.
    recent: VecDeque<Timestamp>,
    /// Cumulative notional of accepted orders in `daily_notional_day` (#41).
    daily_notional: i128,
    /// The day (`now.0 / NANOS_PER_DAY`) `daily_notional` accumulates for. `None` before
    /// the first accepted order.
    daily_notional_day: Option<u64>,
    /// Accepted orders inside the wash-trade window, oldest first, for the pattern check
    /// (#40). Evicted as they age out, same shape as `recent`.
    wash_history: VecDeque<(Timestamp, InstrumentId, Side, Price, Qty)>,
}

impl RiskEngine {
    #[must_use]
    pub fn new(limits: Limits, kill_switch: KillSwitch) -> Self {
        let recent = VecDeque::with_capacity(limits.max_order_rate_per_sec as usize);
        Self {
            limits,
            kill_switch,
            recent,
            daily_notional: 0,
            daily_notional_day: None,
            wash_history: VecDeque::new(),
        }
    }

    #[must_use]
    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    #[must_use]
    pub fn kill_switch(&self) -> &KillSwitch {
        &self.kill_switch
    }

    /// Whether `daily_pnl` is at or past the loss limit, the boundary of check 4.
    /// The engine also asks this after fills, so resting orders cannot run past it.
    #[must_use]
    pub fn daily_loss_reached(&self, daily_pnl: Price) -> bool {
        i128::from(daily_pnl.raw()) <= -i128::from(self.limits.max_daily_loss.raw())
    }

    /// `daily_notional` if `now` is still in `daily_notional_day`, else zero: the day rolled
    /// over. Read-only so [`Self::check_static`] can use it; [`RiskCheck::check`] is what
    /// actually advances the counter once an order is accepted.
    fn daily_notional_so_far(&self, now: Timestamp) -> i128 {
        if self.daily_notional_day == Some(now.0 / NANOS_PER_DAY) {
            self.daily_notional
        } else {
            0
        }
    }

    fn check_static(&self, order: &OrderRequest, ctx: &RiskContext) -> Result<(), RiskReject> {
        let l = &self.limits;
        if order.price <= Price::ZERO || order.qty <= Qty::ZERO {
            return Err(RiskReject::InvalidOrder);
        }
        let age = ctx.now.saturating_since(ctx.last_market_data);
        if age > l.stale_data_ms.saturating_mul(NANOS_PER_MS) {
            return Err(RiskReject::StaleData);
        }
        if self.daily_loss_reached(ctx.daily_pnl) {
            return Err(RiskReject::MaxDailyLoss);
        }
        let reference = ctx
            .reference_price
            .filter(|r| *r > Price::ZERO)
            .ok_or(RiskReject::PriceBand)?;
        let distance = (i128::from(order.price.raw()) - i128::from(reference.raw())).abs();
        if distance * 10_000 > i128::from(l.price_band_bps) * i128::from(reference.raw()) {
            return Err(RiskReject::PriceBand);
        }
        // Self-cross (#40): never trade against our own still-resting order, independent
        // of anything the caller claims its intent was.
        let self_crosses = match order.side {
            Side::Buy => ctx.own_best_ask.is_some_and(|ask| order.price >= ask),
            Side::Sell => ctx.own_best_bid.is_some_and(|bid| order.price <= bid),
        };
        if self_crosses {
            return Err(RiskReject::SelfCross);
        }
        let notional = match order.price.notional(order.qty) {
            Some(n) if n <= l.max_notional => n,
            Some(_) | None => return Err(RiskReject::MaxNotional),
        };
        let daily_after = self.daily_notional_so_far(ctx.now) + i128::from(notional.raw());
        if daily_after > i128::from(l.max_daily_notional.raw()) {
            return Err(RiskReject::MaxDailyNotional);
        }
        let pos = i128::from(ctx.position.raw());
        let qty = i128::from(order.qty.raw());
        let worst = match order.side {
            Side::Buy => pos + i128::from(ctx.open_buy_qty.raw()) + qty,
            Side::Sell => pos - i128::from(ctx.open_sell_qty.raw()) - qty,
        };
        if worst.abs() > i128::from(l.max_position.raw()) {
            return Err(RiskReject::MaxPosition);
        }
        Ok(())
    }

    /// Whether a fill in `wash_history` is the other side of a wash-trade pattern with
    /// `order` (#40): same instrument, opposite side, same price, same quantity (so net
    /// position change is zero) within `wash_trade_window_ms` of `now`. Evaluated against
    /// completed fills, not merely-resting orders: a quote that was canceled unfilled
    /// never traded, so it cannot be the other side of a wash trade. This is what makes a
    /// two-sided market-making quote (bid and ask resting at once, one later canceled) safe
    /// on its own; a real wash-trade pattern must have actually executed on both sides.
    fn wash_trade_detected(&self, order: &OrderRequest, now: Timestamp) -> bool {
        let window = self
            .limits
            .wash_trade_window_ms
            .saturating_mul(NANOS_PER_MS);
        let window_start = now.0.saturating_sub(window);
        self.wash_history
            .iter()
            .any(|&(t, instrument, side, price, qty)| {
                t.0 > window_start
                    && instrument == order.instrument
                    && side != order.side
                    && price == order.price
                    && qty == order.qty
            })
    }

    /// Records a completed fill for wash-trade pattern detection (#40). Callers (the
    /// bridge engine, the replay engine) call this once a venue confirms a fill, using the
    /// filled quantity and price, not the order's original size — a partial fill is what
    /// actually changed position, and that is what a reversal must match.
    pub fn record_fill(
        &mut self,
        instrument: InstrumentId,
        side: Side,
        price: Price,
        qty: Qty,
        ts: Timestamp,
    ) {
        let window_start = ts.0.saturating_sub(
            self.limits
                .wash_trade_window_ms
                .saturating_mul(NANOS_PER_MS),
        );
        while self
            .wash_history
            .front()
            .is_some_and(|(t, ..)| t.0 <= window_start)
        {
            self.wash_history.pop_front();
        }
        self.wash_history
            .push_back((ts, instrument, side, price, qty));
    }
}

impl RiskCheck for RiskEngine {
    fn check(&mut self, order: &OrderRequest, ctx: &RiskContext) -> Result<(), RiskReject> {
        if self.kill_switch.is_engaged() {
            return Err(RiskReject::KillSwitch);
        }
        self.check_static(order, ctx)?;
        if self.wash_trade_detected(order, ctx.now) {
            return Err(RiskReject::WashTrade);
        }
        let window_start = ctx.now.0.saturating_sub(NANOS_PER_SEC);
        while self.recent.front().is_some_and(|t| t.0 <= window_start) {
            self.recent.pop_front();
        }
        if self.recent.len() >= self.limits.max_order_rate_per_sec as usize {
            return Err(RiskReject::MaxOrderRate);
        }
        self.recent.push_back(ctx.now);

        // Only an order that passed every check above counts toward the daily notional
        // cap, same principle as the rate window just above.
        let notional = order
            .price
            .notional(order.qty)
            .expect("check_static already proved this fits in a Price");
        self.daily_notional = self.daily_notional_so_far(ctx.now) + i128::from(notional.raw());
        self.daily_notional_day = Some(ctx.now.0 / NANOS_PER_DAY);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use qc_core::{ClientOrderId, InstrumentId, OrderType, StrategyId};

    fn limits() -> Limits {
        toml::from_str(include_str!("../../../../config/limits/default.toml")).unwrap()
    }

    fn p(s: &str) -> Price {
        s.parse().unwrap()
    }

    fn q(s: &str) -> Qty {
        s.parse().unwrap()
    }

    const SEC: u64 = 1_000_000_000;

    /// Defaults: `max_position` 0.01, `max_notional` 25, 5 orders/s, `max_daily_loss` 5,
    /// band 50 bps, stale 1000 ms, `max_daily_notional` 50, wash window 2000 ms. The base
    /// order is well inside every limit.
    fn order(side: Side, price: &str, qty: &str) -> OrderRequest {
        OrderRequest {
            client_id: ClientOrderId(1),
            strategy: StrategyId(1),
            instrument: InstrumentId(1),
            side,
            order_type: OrderType::Limit,
            price: p(price),
            qty: q(qty),
        }
    }

    fn ctx() -> RiskContext {
        RiskContext {
            now: Timestamp(10 * SEC),
            last_market_data: Timestamp(10 * SEC),
            position: Qty::ZERO,
            open_buy_qty: Qty::ZERO,
            open_sell_qty: Qty::ZERO,
            reference_price: Some(p("10000")),
            daily_pnl: Price::ZERO,
            own_best_bid: None,
            own_best_ask: None,
        }
    }

    fn engine() -> RiskEngine {
        RiskEngine::new(limits(), KillSwitch::new())
    }

    fn check(o: &OrderRequest, c: &RiskContext) -> Result<(), RiskReject> {
        engine().check(o, c)
    }

    #[test]
    fn default_limits_file_parses_and_rejects_unknown_keys() {
        let text = include_str!("../../../../config/limits/default.toml");
        let limits: Limits = toml::from_str(text).unwrap();
        assert!(limits.max_position > Qty::ZERO);
        assert!(limits.max_notional > Price::ZERO);
        assert!(toml::from_str::<Limits>(&format!("{text}\nmax_leverage = 100\n")).is_err());
    }

    /// #41 / root CLAUDE.md rule 4: every default in `config/limits/default.toml` is
    /// deliberately tiny (the file's own header says so), and a brand-new limit key gets a
    /// conservative placeholder pending #23 rather than a value sized to fit a test fixture.
    /// `max_daily_notional` must not be an outlier next to `max_notional` on the same file:
    /// a cumulative daily cap that is hundreds of times a single order's cap gives a real
    /// agentic account no practical protection at all. `tests/replay/sample_day.csv`'s
    /// higher-volume replay gets its own fixture-only limits file instead of inflating this
    /// one (see `tests/replay/limits.toml`).
    #[test]
    fn default_daily_notional_cap_is_conservative_not_sized_to_a_test_fixture() {
        let limits: Limits =
            toml::from_str(include_str!("../../../../config/limits/default.toml")).unwrap();
        assert!(
            limits.max_daily_notional.raw() <= limits.max_notional.raw() * 10,
            "max_daily_notional ({}) must be at most a small multiple of max_notional ({}), \
             not a value picked to avoid tripping a replay fixture",
            limits.max_daily_notional,
            limits.max_notional
        );
    }

    #[test]
    fn crate_has_no_features_that_could_switch_checks_off() {
        assert!(!include_str!("../Cargo.toml").contains("[features]"));
    }

    #[test]
    fn kill_switch_is_checked_first_and_shared_across_clones() {
        let mut risk = engine();
        let remote = risk.kill_switch().clone();
        let good = order(Side::Buy, "10000", "0.001");
        assert_eq!(risk.check(&good, &ctx()), Ok(()));
        remote.engage();
        assert_eq!(risk.check(&good, &ctx()), Err(RiskReject::KillSwitch));
        // Even an order that breaks every other limit reports the kill switch.
        let mut awful = ctx();
        awful.last_market_data = Timestamp(0);
        awful.daily_pnl = p("-1000");
        assert_eq!(
            risk.check(&order(Side::Buy, "0", "0"), &awful),
            Err(RiskReject::KillSwitch)
        );
    }

    #[test]
    fn invalid_orders_are_rejected() {
        for (price, qty) in [("0", "0.001"), ("10000", "0"), ("10000", "-0.001")] {
            assert_eq!(
                check(&order(Side::Buy, price, qty), &ctx()),
                Err(RiskReject::InvalidOrder)
            );
        }
    }

    #[test]
    fn stale_data_boundary() {
        let o = order(Side::Buy, "10000", "0.001");
        let mut c = ctx();
        c.last_market_data = Timestamp(c.now.0 - 1000 * 1_000_000);
        assert_eq!(check(&o, &c), Ok(()), "exactly stale_data_ms old passes");
        c.last_market_data.0 -= 1;
        assert_eq!(check(&o, &c), Err(RiskReject::StaleData));
        c.last_market_data = Timestamp(0);
        assert_eq!(
            check(&o, &c),
            Err(RiskReject::StaleData),
            "no data at all is stale"
        );
    }

    #[test]
    fn daily_loss_boundary() {
        let o = order(Side::Buy, "10000", "0.001");
        let mut c = ctx();
        c.daily_pnl = Price::from_raw(-p("5").raw() + 1);
        assert_eq!(check(&o, &c), Ok(()), "one unit short of the loss passes");
        c.daily_pnl = p("-5");
        assert_eq!(
            check(&o, &c),
            Err(RiskReject::MaxDailyLoss),
            "reaching the loss halts"
        );
        c.daily_pnl = p("-5.00000001");
        assert_eq!(check(&o, &c), Err(RiskReject::MaxDailyLoss));
    }

    #[test]
    fn price_band_boundary() {
        // 50 bps of 10000 is 50.
        for (price, ok) in [
            ("10050", true),
            ("10050.00000001", false),
            ("9950", true),
            ("9949.99999999", false),
        ] {
            let got = check(&order(Side::Sell, price, "0.001"), &ctx());
            assert_eq!(got.is_ok(), ok, "{price}: {got:?}");
            if !ok {
                assert_eq!(got, Err(RiskReject::PriceBand));
            }
        }
        let mut c = ctx();
        c.reference_price = None;
        assert_eq!(
            check(&order(Side::Buy, "10000", "0.001"), &c),
            Err(RiskReject::PriceBand)
        );
    }

    #[test]
    fn notional_boundary() {
        let mut c = ctx();
        c.reference_price = Some(p("2500"));
        // 2500 * 0.01 = 25 exactly; position limit is also 0.01, so both are at the edge.
        assert_eq!(check(&order(Side::Buy, "2500", "0.01"), &c), Ok(()));
        assert_eq!(
            check(&order(Side::Buy, "2500.00000100", "0.01"), &c),
            Err(RiskReject::MaxNotional)
        );
        c.reference_price = Some(Price::from_raw(i64::MAX / 2));
        assert_eq!(
            check(
                &order(
                    Side::Buy,
                    &Price::from_raw(i64::MAX / 2).to_string(),
                    "1000"
                ),
                &c
            ),
            Err(RiskReject::MaxNotional),
            "overflow rejects"
        );
    }

    #[test]
    fn position_boundary_counts_open_orders_on_the_same_side() {
        // Priced at 2000 so even the 0.01 sell ($20) fits the $25 per-order cap.
        let mut c = ctx();
        c.reference_price = Some(p("2000"));
        c.position = q("0.005");
        c.open_buy_qty = q("0.004");
        assert_eq!(check(&order(Side::Buy, "2000", "0.001"), &c), Ok(()));
        assert_eq!(
            check(&order(Side::Buy, "2000", "0.00100001"), &c),
            Err(RiskReject::MaxPosition)
        );
        // Selling reduces a long, so it passes even though buys are at the limit.
        assert_eq!(check(&order(Side::Sell, "2000", "0.01"), &c), Ok(()));
        c.position = q("-0.005");
        c.open_sell_qty = q("0.005");
        assert_eq!(
            check(&order(Side::Sell, "2000", "0.00000001"), &c),
            Err(RiskReject::MaxPosition)
        );
        c.open_sell_qty = q("0.004");
        assert_eq!(check(&order(Side::Sell, "2000", "0.001"), &c), Ok(()));
    }

    #[test]
    fn order_rate_boundary_uses_a_sliding_one_second_window() {
        let mut risk = engine();
        // $1 each, so every accepted order here stays under the $50 daily cap.
        let o = order(Side::Buy, "10000", "0.0001");
        let at = |ns: u64| {
            let mut c = ctx();
            c.now = Timestamp(ns);
            c.last_market_data = Timestamp(ns);
            c
        };
        let t0 = 10 * SEC;
        for i in 0..5 {
            assert_eq!(risk.check(&o, &at(t0 + i)), Ok(()), "order {i} of 5");
        }
        assert_eq!(risk.check(&o, &at(t0 + 5)), Err(RiskReject::MaxOrderRate));
        // The first order leaves the window exactly 1 s after it passed.
        assert_eq!(
            risk.check(&o, &at(t0 + SEC - 1)),
            Err(RiskReject::MaxOrderRate)
        );
        assert_eq!(risk.check(&o, &at(t0 + SEC)), Ok(()));
        assert_eq!(risk.check(&o, &at(t0 + SEC)), Err(RiskReject::MaxOrderRate));
    }

    #[test]
    fn rejected_orders_do_not_use_up_the_rate_limit() {
        let mut risk = engine();
        let bad = order(Side::Buy, "20000", "0.001");
        for _ in 0..10 {
            assert_eq!(risk.check(&bad, &ctx()), Err(RiskReject::PriceBand));
        }
        for _ in 0..5 {
            assert_eq!(
                risk.check(&order(Side::Buy, "10000", "0.001"), &ctx()),
                Ok(())
            );
        }
    }

    /// #40: never trade against our own still-resting order, regardless of what the
    /// caller says its intent was.
    #[test]
    fn self_cross_rejects_an_order_that_would_fill_against_our_own_resting_order() {
        let mut c = ctx();
        c.own_best_ask = Some(p("10000"));
        // Buying at or above our own resting ask would fill against ourselves.
        assert_eq!(
            check(&order(Side::Buy, "10000", "0.001"), &c),
            Err(RiskReject::SelfCross),
            "at our own ask crosses"
        );
        assert_eq!(
            check(&order(Side::Buy, "10000.00000001", "0.001"), &c),
            Err(RiskReject::SelfCross),
            "more aggressive than our own ask crosses"
        );
        assert_eq!(
            check(&order(Side::Buy, "9999.99999999", "0.001"), &c),
            Ok(()),
            "one tick less aggressive than our own ask does not cross"
        );

        let mut c = ctx();
        c.own_best_bid = Some(p("10000"));
        assert_eq!(
            check(&order(Side::Sell, "10000", "0.001"), &c),
            Err(RiskReject::SelfCross),
            "at our own bid crosses"
        );
        assert_eq!(
            check(&order(Side::Sell, "9999.99999999", "0.001"), &c),
            Err(RiskReject::SelfCross),
            "more aggressive than our own bid crosses"
        );
        assert_eq!(
            check(&order(Side::Sell, "10000.00000001", "0.001"), &c),
            Ok(()),
            "one tick less aggressive than our own bid does not cross"
        );
    }

    /// #40: a buy then a same-price, same-qty sell (or the reverse) within the
    /// configured window is a wash-trade pattern, flagged/rejected with the reason
    /// recorded, independent of the rate limit and of stated intent.
    #[test]
    fn wash_trade_pattern_rejects_a_same_price_same_qty_reversal_inside_the_window() {
        let mut risk = engine();
        let sell = order(Side::Sell, "10000", "0.001");
        let mut c = ctx();
        // A fill, not merely an accepted order, is what the wash-trade check reacts to.
        risk.record_fill(InstrumentId(1), Side::Buy, p("10000"), q("0.001"), c.now);

        // Immediately after: rejected.
        assert_eq!(risk.check(&sell, &c), Err(RiskReject::WashTrade));

        let window_ns = limits().wash_trade_window_ms * 1_000_000;
        // One ns inside the window: still rejected.
        c.now = Timestamp(c.now.0 + window_ns - 1);
        c.last_market_data = c.now;
        assert_eq!(risk.check(&sell, &c), Err(RiskReject::WashTrade));

        // Exactly at the window: the pattern has aged out, same boundary the rate
        // limiter uses.
        c.now = Timestamp(c.now.0 + 1);
        c.last_market_data = c.now;
        assert_eq!(risk.check(&sell, &c), Ok(()));

        // A same-side fill is never a wash trade, no matter how fast.
        let mut risk = engine();
        risk.record_fill(
            InstrumentId(1),
            Side::Buy,
            p("10000"),
            q("0.001"),
            ctx().now,
        );
        assert_eq!(
            risk.check(&order(Side::Buy, "10000", "0.001"), &ctx()),
            Ok(())
        );

        // Opposite side but a different price or qty is not the pattern either.
        let mut risk = engine();
        risk.record_fill(
            InstrumentId(1),
            Side::Buy,
            p("10000"),
            q("0.001"),
            ctx().now,
        );
        assert_eq!(
            risk.check(&order(Side::Sell, "10000.00000001", "0.001"), &ctx()),
            Ok(()),
            "different price is not a wash trade"
        );
        assert_eq!(
            risk.check(&order(Side::Sell, "10000", "0.0009999"), &ctx()),
            Ok(()),
            "different qty is not a wash trade"
        );
    }

    /// #40: a two-sided market-making quote (a resting bid and a resting ask at once,
    /// with one later canceled before it ever fills) is not a wash trade. A wash trade is
    /// about completed round-trip trades, not merely about quoting both sides; a canceled,
    /// unfilled order never traded, so a later order that happens to match its price and
    /// qty is not a reversal of anything real.
    #[test]
    fn an_accepted_but_never_filled_order_is_not_wash_trade_history() {
        let mut risk = engine();
        let c = ctx();
        // A resting sell accepted (not filled) ...
        assert_eq!(risk.check(&order(Side::Sell, "10000", "0.001"), &c), Ok(()));
        // ... then canceled unfilled: no record_fill call ever happens for it.
        // A same-price, same-qty buy right after is ordinary two-sided quoting, not wash
        // trading, since nothing on the sell side ever executed.
        assert_eq!(risk.check(&order(Side::Buy, "10000", "0.001"), &c), Ok(()));
    }

    /// #41: the per-order cap (`max_notional`) and the cumulative daily cap
    /// (`max_daily_notional`) are independent; a sequence of orders each within the
    /// per-order cap can still cross the daily cap.
    #[test]
    fn daily_notional_cap_rejects_once_cumulative_total_crosses_it() {
        let mut risk = engine();
        let mut c = ctx();
        c.reference_price = Some(p("2500"));
        // Notional 2500 * 0.01 = 25 exactly: at the per-order cap, not over it.
        let o = order(Side::Buy, "2500", "0.01");
        let day_start = c.now.0;
        // Default max_daily_notional is 50: two of these reach it exactly. Time advances
        // 1 s per order, well inside the same exchange day, so the rate limit (5/s) never
        // interferes.
        for i in 0..2 {
            c.now = Timestamp(day_start + i * SEC);
            c.last_market_data = c.now;
            assert_eq!(
                risk.check(&o, &c),
                Ok(()),
                "order {i} of 2 reaches the cap exactly"
            );
        }
        // One more, of any size, would push the cumulative total past the cap.
        let tiny = order(Side::Buy, "2500", "0.00000001");
        assert_eq!(risk.check(&tiny, &c), Err(RiskReject::MaxDailyNotional));

        // A new exchange day resets the counter (placeholder bucketing pending #23).
        c.now = Timestamp((c.now.0 / NANOS_PER_DAY + 1) * NANOS_PER_DAY);
        c.last_market_data = c.now;
        assert_eq!(
            risk.check(&o, &c),
            Ok(()),
            "the daily counter reset on the new day"
        );
    }
}
