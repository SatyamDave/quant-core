//! A deterministic in-memory venue for replay, tests and chaos scenarios.
//!
//! Matching model, deliberately simple (fill modelling proper lives in `backtest/`):
//! - A marketable order fills immediately against the displayed opposite levels
//!   at their prices, up to its limit. Each fill consumes displayed size at that
//!   level (tracked separately from the book, which only the feed itself moves),
//!   so a second aggressive order arriving before the next book update sees the
//!   remaining size, not the full displayed size again.
//! - Resting orders fill only when a recorded trade prints at or through their
//!   price with the opposite aggressor, best price first then oldest first, up to
//!   the trade's size. A resting order also gets a queue position: the size
//!   displayed on its own side at its own price when it arrived. That much
//!   volume must trade at or through the price before the order gets any fill.
//! - `PostOnly` that would take liquidity is rejected; the `Ioc` remainder is canceled.
//!
//! Venue time is the local timestamp of the last record fed in.

use std::collections::{BTreeMap, VecDeque};

use qc_core::{
    ClientOrderId, Event, InstrumentId, MarketEvent, OrderEvent, OrderRequest, OrderType, Price,
    Qty, Side, Timestamp, VenueId, VenueOrderId,
};
use qc_oms::VenueOrder;
use qc_orderbook::{OrderBook, better};

use crate::record::Record;
use crate::{GatewayError, VenueAdapter};

#[derive(Debug, Clone, Copy)]
struct SimOrder {
    client_id: ClientOrderId,
    venue_id: VenueOrderId,
    side: Side,
    price: Price,
    qty: Qty,
    filled: Qty,
    open: bool,
    /// Displayed size ahead of this order at its price when it arrived. A
    /// recorded trade at or through the price must exhaust this before any of
    /// it can fill this order (queue position, best-price/oldest-first is the
    /// rest of the priority; see [`SimVenue::match_trade`]).
    queue_ahead: Qty,
}

#[derive(Debug)]
pub struct SimVenue {
    venue: VenueId,
    book: OrderBook,
    connected: bool,
    now: Timestamp,
    next_venue_id: u64,
    /// Every order ever accepted, by venue ID, so reconciliation can see finished ones too.
    orders: BTreeMap<VenueOrderId, SimOrder>,
    by_client: BTreeMap<ClientOrderId, VenueOrderId>,
    events: VecDeque<Event>,
    /// Size already taken from a displayed level by an aggressive order since
    /// the last book update touched that (side, price) level. Cleared for a
    /// level as soon as a snapshot or delta replaces it, since the fresh
    /// displayed size then reflects the world's current view.
    consumed: BTreeMap<(Side, Price), Qty>,
}

impl SimVenue {
    #[must_use]
    pub fn new(venue: VenueId, instrument: InstrumentId) -> Self {
        Self {
            venue,
            book: OrderBook::new(instrument),
            connected: true,
            now: Timestamp(0),
            next_venue_id: 1,
            orders: BTreeMap::new(),
            by_client: BTreeMap::new(),
            events: VecDeque::new(),
            consumed: BTreeMap::new(),
        }
    }

    /// Simulates losing or regaining the connection. While disconnected,
    /// submit and cancel fail and `poll` returns nothing; the venue keeps
    /// matching resting orders and delivers the queued events on reconnect.
    pub fn set_connected(&mut self, connected: bool) {
        self.connected = connected;
    }

    #[must_use]
    pub fn book(&self) -> &OrderBook {
        &self.book
    }

    /// Feeds one recorded record to the venue's own view of the market.
    /// A broken feed (gap, cross) leaves the venue book waiting for a snapshot,
    /// just as the engine's book would.
    pub fn on_record(&mut self, record: &Record) {
        self.now = self.now.max(record.ts_local());
        match record {
            Record::Snapshot(s) => {
                if self.book.apply_snapshot(s).is_ok() {
                    // A snapshot replaces every level on both sides.
                    self.consumed.clear();
                }
            }
            Record::Market(MarketEvent::BookDelta(d)) => {
                if self.book.apply(d).is_ok() {
                    // This exact level was just replaced by the feed; any size an
                    // aggressive order took from it no longer applies.
                    self.consumed.remove(&(d.side, d.price));
                }
            }
            Record::Market(MarketEvent::Trade(t)) => {
                if t.instrument == self.book.instrument() {
                    self.match_trade(t.aggressor, t.price, t.qty);
                }
            }
        }
    }

    fn match_trade(&mut self, aggressor: Side, price: Price, mut size: Qty) {
        let resting_side = match aggressor {
            Side::Buy => Side::Sell,
            Side::Sell => Side::Buy,
        };
        // Priority: best price first, then oldest (lowest venue ID).
        let mut hit: Vec<(Price, VenueOrderId)> = self
            .orders
            .values()
            .filter(|o| o.open && o.side == resting_side && !better(resting_side, price, o.price))
            .map(|o| (o.price, o.venue_id))
            .collect();
        hit.sort_by(|a, b| match resting_side {
            Side::Buy => b.0.cmp(&a.0).then(a.1.cmp(&b.1)),
            Side::Sell => a.0.cmp(&b.0).then(a.1.cmp(&b.1)),
        });
        for (_, id) in hit {
            if size <= Qty::ZERO {
                break;
            }
            let order = self.orders.get_mut(&id).expect("id came from the map");
            // This trade first has to clear whatever was displayed ahead of the
            // order when it arrived; only volume beyond that can fill it.
            let ahead = order.queue_ahead.min(size);
            order.queue_ahead = Qty::from_raw(order.queue_ahead.raw() - ahead.raw());
            size = Qty::from_raw(size.raw() - ahead.raw());
            if size <= Qty::ZERO {
                continue;
            }
            let open = Qty::from_raw(order.qty.raw() - order.filled.raw());
            let take = open.min(size);
            size = Qty::from_raw(size.raw() - take.raw());
            let order_price = order.price;
            self.fill(id, order_price, take);
        }
    }

    fn fill(&mut self, id: VenueOrderId, price: Price, qty: Qty) {
        let order = self.orders.get_mut(&id).expect("caller holds a valid id");
        order.filled = Qty::from_raw(order.filled.raw() + qty.raw());
        if order.filled == order.qty {
            order.open = false;
        }
        self.events.push_back(Event::Order(OrderEvent::Filled {
            client_id: order.client_id,
            price,
            qty,
            ts: self.now,
        }));
    }

    /// Every order the venue has accepted, as its order-status endpoint would report them.
    #[must_use]
    pub fn orders(&self) -> Vec<VenueOrder> {
        self.orders
            .values()
            .map(|o| VenueOrder {
                venue_id: o.venue_id,
                client_id: Some(o.client_id),
                open: o.open,
                filled: o.filled,
            })
            .collect()
    }

    /// Places an order nobody in this process sent, as a human with the same key might.
    pub fn inject_foreign_order(&mut self, side: Side, price: Price, qty: Qty) -> VenueOrderId {
        let venue_id = VenueOrderId(self.next_venue_id);
        self.next_venue_id += 1;
        self.orders.insert(
            venue_id,
            SimOrder {
                client_id: ClientOrderId(u64::MAX - venue_id.0),
                venue_id,
                side,
                price,
                qty,
                filled: Qty::ZERO,
                open: true,
                queue_ahead: Qty::ZERO,
            },
        );
        venue_id
    }
}

impl VenueAdapter for SimVenue {
    fn venue(&self) -> VenueId {
        self.venue
    }

    fn submit(&mut self, order: &OrderRequest) -> Result<(), GatewayError> {
        if !self.connected {
            return Err(GatewayError::Disconnected);
        }
        if order.instrument != self.book.instrument() {
            return Err(GatewayError::UnknownInstrument(order.instrument));
        }
        let (client_id, ts) = (order.client_id, self.now);
        if self.by_client.contains_key(&client_id) || order.qty <= Qty::ZERO {
            self.events
                .push_back(Event::Order(OrderEvent::Rejected { client_id, ts }));
            return Ok(());
        }
        let opposite = match order.side {
            Side::Buy => Side::Sell,
            Side::Sell => Side::Buy,
        };
        let crosses = |p: Price| !better(order.side, p, order.price);
        let marketable = self
            .book
            .levels(opposite)
            .first()
            .is_some_and(|(p, _)| crosses(*p));
        if marketable && order.order_type == OrderType::PostOnly {
            self.events
                .push_back(Event::Order(OrderEvent::Rejected { client_id, ts }));
            return Ok(());
        }
        let venue_id = VenueOrderId(self.next_venue_id);
        self.next_venue_id += 1;
        // Queue position: the size already displayed on our own side at our own
        // price when we arrive. A resting order joins the back of that queue.
        let queue_ahead = self
            .book
            .levels(order.side)
            .iter()
            .find(|(p, _)| *p == order.price)
            .map_or(Qty::ZERO, |(_, q)| *q);
        self.orders.insert(
            venue_id,
            SimOrder {
                client_id,
                venue_id,
                side: order.side,
                price: order.price,
                qty: order.qty,
                filled: Qty::ZERO,
                open: true,
                queue_ahead,
            },
        );
        self.by_client.insert(client_id, venue_id);
        self.events.push_back(Event::Order(OrderEvent::Accepted {
            client_id,
            venue_id,
            ts,
        }));
        let takes: Vec<(Price, Qty)> = self
            .book
            .levels(opposite)
            .iter()
            .take_while(|(p, _)| crosses(*p))
            .copied()
            .collect();
        let mut remaining = order.qty;
        for (price, displayed) in takes {
            if remaining <= Qty::ZERO {
                break;
            }
            let already_taken = self
                .consumed
                .get(&(opposite, price))
                .copied()
                .unwrap_or(Qty::ZERO);
            let available = Qty::from_raw((displayed.raw() - already_taken.raw()).max(0));
            if available <= Qty::ZERO {
                continue;
            }
            let take = available.min(remaining);
            remaining = Qty::from_raw(remaining.raw() - take.raw());
            self.consumed.insert(
                (opposite, price),
                Qty::from_raw(already_taken.raw() + take.raw()),
            );
            self.fill(venue_id, price, take);
        }
        let order_type = order.order_type;
        let still_open = self.orders[&venue_id].open;
        match order_type {
            OrderType::Ioc if still_open => {
                self.orders.get_mut(&venue_id).expect("just inserted").open = false;
                self.events
                    .push_back(Event::Order(OrderEvent::Canceled { client_id, ts }));
            }
            OrderType::Ioc | OrderType::Limit | OrderType::PostOnly => {}
        }
        Ok(())
    }

    fn cancel(&mut self, client_id: ClientOrderId) -> Result<(), GatewayError> {
        if !self.connected {
            return Err(GatewayError::Disconnected);
        }
        let ts = self.now;
        let open = self
            .by_client
            .get(&client_id)
            .and_then(|id| self.orders.get_mut(id))
            .filter(|o| o.open);
        let event = if let Some(order) = open {
            order.open = false;
            OrderEvent::Canceled { client_id, ts }
        } else {
            OrderEvent::CancelRejected { client_id, ts }
        };
        self.events.push_back(Event::Order(event));
        Ok(())
    }

    fn order_snapshot(&mut self) -> Result<Vec<VenueOrder>, GatewayError> {
        if self.connected {
            Ok(self.orders())
        } else {
            Err(GatewayError::Disconnected)
        }
    }

    fn poll(&mut self) -> Option<Event> {
        if self.connected {
            self.events.pop_front()
        } else {
            None
        }
    }
}
