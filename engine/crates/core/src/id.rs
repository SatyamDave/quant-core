//! Newtype IDs so an instrument can never be passed where an order is expected.

macro_rules! id_type {
    ($(#[$doc:meta])* $name:ident($inner:ty)) => {
        $(#[$doc])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(pub $inner);
    };
}

id_type!(
    /// An instrument, assigned by the engine's instrument table.
    InstrumentId(u32)
);
id_type!(
    /// A trading venue.
    VenueId(u16)
);
id_type!(
    /// A strategy instance; each has its own venue keys and limits.
    StrategyId(u16)
);
id_type!(
    /// An order ID the engine assigns before sending, unique per strategy.
    ClientOrderId(u64)
);
id_type!(
    /// The order ID the venue assigns on acceptance.
    VenueOrderId(u64)
);
