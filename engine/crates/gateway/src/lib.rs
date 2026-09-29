//! The adapter trait every venue module implements. Credentials are injected at
//! runtime by the caller, never read from files in the repository.

pub mod pacing;
pub mod record;
pub mod sim;

pub use pacing::{TokenBucket, reconnect_delay_ms};
pub use record::{ParseError, Record};
pub use sim::SimVenue;

use qc_core::{ClientOrderId, Event, InstrumentId, OrderRequest, VenueId};
use qc_oms::VenueOrder;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GatewayError {
    Disconnected,
    /// The venue's rate limit would be exceeded; retry after the given delay.
    RateLimited {
        retry_after_ms: u64,
    },
    UnknownInstrument(InstrumentId),
    Venue(String),
}

/// One connection to one venue. Implementations handle reconnect with backoff
/// and snapshot plus delta sync internally.
pub trait VenueAdapter {
    fn venue(&self) -> VenueId;

    /// # Errors
    /// Returns [`GatewayError`] if the order could not be handed to the venue.
    fn submit(&mut self, order: &OrderRequest) -> Result<(), GatewayError>;

    /// # Errors
    /// Returns [`GatewayError`] if the cancel could not be handed to the venue.
    fn cancel(&mut self, client_id: ClientOrderId) -> Result<(), GatewayError>;

    /// Next market or order event, if one is ready. Never blocks.
    fn poll(&mut self) -> Option<Event>;

    /// Every order the venue knows for this key, for reconciliation.
    ///
    /// # Errors
    /// Returns [`GatewayError`] if the venue could not be asked.
    fn order_snapshot(&mut self) -> Result<Vec<VenueOrder>, GatewayError>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use qc_core::{OrderEvent, OrderType, Price, Qty, Side, StrategyId, Timestamp};

    /// Rejects everything; proves the trait is object-safe and usable.
    struct Offline;

    impl VenueAdapter for Offline {
        fn venue(&self) -> VenueId {
            VenueId(0)
        }
        fn submit(&mut self, _: &OrderRequest) -> Result<(), GatewayError> {
            Err(GatewayError::Disconnected)
        }
        fn cancel(&mut self, _: ClientOrderId) -> Result<(), GatewayError> {
            Err(GatewayError::Disconnected)
        }
        fn order_snapshot(&mut self) -> Result<Vec<VenueOrder>, GatewayError> {
            Err(GatewayError::Disconnected)
        }
        fn poll(&mut self) -> Option<Event> {
            Some(Event::Order(OrderEvent::Canceled {
                client_id: ClientOrderId(1),
                ts: Timestamp(0),
            }))
        }
    }

    #[test]
    fn adapter_is_object_safe() {
        let mut adapter: Box<dyn VenueAdapter> = Box::new(Offline);
        let order = OrderRequest {
            client_id: ClientOrderId(1),
            strategy: StrategyId(1),
            instrument: InstrumentId(1),
            side: Side::Buy,
            order_type: OrderType::PostOnly,
            price: Price::from_raw(1),
            qty: Qty::from_raw(1),
        };
        assert_eq!(adapter.submit(&order), Err(GatewayError::Disconnected));
        assert!(adapter.poll().is_some());
    }
}
