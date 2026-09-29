//! Everything the engine reacts to is an [`Event`]; everything it sends is an [`OrderRequest`].

use crate::{ClientOrderId, InstrumentId, Price, Qty, StrategyId, Timestamp, VenueOrderId};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Side {
    Buy,
    Sell,
}

/// One price level changed. `qty == 0` removes the level.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BookDelta {
    pub instrument: InstrumentId,
    pub side: Side,
    pub price: Price,
    pub qty: Qty,
    /// Venue sequence number; a gap means the book must resync.
    pub seq: u64,
    pub ts_exchange: Timestamp,
    pub ts_local: Timestamp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Trade {
    pub instrument: InstrumentId,
    pub aggressor: Side,
    pub price: Price,
    pub qty: Qty,
    pub ts_exchange: Timestamp,
    pub ts_local: Timestamp,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarketEvent {
    BookDelta(BookDelta),
    Trade(Trade),
}

/// Venue responses about our own orders.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderEvent {
    Accepted {
        client_id: ClientOrderId,
        venue_id: VenueOrderId,
        ts: Timestamp,
    },
    Rejected {
        client_id: ClientOrderId,
        ts: Timestamp,
    },
    Filled {
        client_id: ClientOrderId,
        price: Price,
        qty: Qty,
        ts: Timestamp,
    },
    Canceled {
        client_id: ClientOrderId,
        ts: Timestamp,
    },
    /// The venue refused a cancel, usually because the order already filled.
    CancelRejected {
        client_id: ClientOrderId,
        ts: Timestamp,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Event {
    Market(MarketEvent),
    Order(OrderEvent),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OrderType {
    Limit,
    /// Limit order that is rejected rather than taking liquidity.
    PostOnly,
    /// Immediate-or-cancel limit order.
    Ioc,
}

/// An order a strategy wants to send. It reaches a venue only after risk passes it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct OrderRequest {
    pub client_id: ClientOrderId,
    pub strategy: StrategyId,
    pub instrument: InstrumentId,
    pub side: Side,
    pub order_type: OrderType,
    pub price: Price,
    pub qty: Qty,
}
