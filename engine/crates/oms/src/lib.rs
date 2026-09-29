//! Order lifecycle and venue reconciliation. Matches on [`OrderState`] and
//! [`Transition`] must list every variant: no `_` arms, so adding a state or an
//! event forces every transition to be decided.

use std::collections::{BTreeMap, BTreeSet};

use qc_core::{ClientOrderId, OrderEvent, OrderRequest, Qty, VenueOrderId};

/// Where an order is in its life, as the engine currently believes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum OrderState {
    /// Sent to the venue, no acknowledgement yet.
    PendingNew,
    /// Resting on the venue's book.
    New,
    PartiallyFilled,
    Filled,
    /// Cancel sent, no acknowledgement yet.
    PendingCancel,
    Canceled,
    Rejected,
}

impl OrderState {
    pub const ALL: [OrderState; 7] = [
        Self::PendingNew,
        Self::New,
        Self::PartiallyFilled,
        Self::Filled,
        Self::PendingCancel,
        Self::Canceled,
        Self::Rejected,
    ];

    /// A terminal order can never change again.
    #[must_use]
    pub const fn is_terminal(self) -> bool {
        match self {
            Self::Filled | Self::Canceled | Self::Rejected => true,
            Self::PendingNew | Self::New | Self::PartiallyFilled | Self::PendingCancel => false,
        }
    }
}

/// What can happen to an order, reduced to what the state machine needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Transition {
    Accepted,
    Rejected,
    /// A fill that leaves quantity open.
    PartialFill,
    /// A fill that completes the order.
    FinalFill,
    /// We asked the venue to cancel.
    CancelRequested,
    /// The venue canceled the order (on our request or its own, e.g. an IOC remainder).
    Canceled,
    CancelRejected,
}

impl Transition {
    pub const ALL: [Transition; 7] = [
        Self::Accepted,
        Self::Rejected,
        Self::PartialFill,
        Self::FinalFill,
        Self::CancelRequested,
        Self::Canceled,
        Self::CancelRejected,
    ];
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OmsError {
    Illegal {
        client_id: ClientOrderId,
        state: OrderState,
        transition: Transition,
    },
    UnknownOrder(ClientOrderId),
    DuplicateOrder(ClientOrderId),
    /// Fills added up to more than the order quantity.
    Overfill(ClientOrderId),
}

/// The order state machine. `has_fills` only matters when a cancel is
/// refused: the order goes back to `PartiallyFilled` if anything filled, else `New`.
///
/// Rules worth knowing: fills may race a pending cancel; an ack may arrive
/// after we already asked to cancel; the venue may cancel on its own (IOC);
/// nothing leaves a terminal state. Returns `None` for an illegal transition.
#[must_use]
pub const fn transition(state: OrderState, t: Transition, has_fills: bool) -> Option<OrderState> {
    use OrderState as S;
    use Transition as T;
    let resting = if has_fills {
        S::PartiallyFilled
    } else {
        S::New
    };
    match (state, t) {
        (S::PendingNew, T::Accepted) => Some(S::New),
        (S::PendingNew | S::PendingCancel, T::Rejected) => Some(S::Rejected),
        (S::PendingNew | S::New | S::PartiallyFilled, T::PartialFill) => Some(S::PartiallyFilled),
        (S::PendingNew | S::New | S::PartiallyFilled | S::PendingCancel, T::FinalFill) => {
            Some(S::Filled)
        }
        (S::PendingNew | S::New | S::PartiallyFilled | S::PendingCancel, T::Canceled) => {
            Some(S::Canceled)
        }
        (S::PendingNew | S::New | S::PartiallyFilled, T::CancelRequested)
        | (S::PendingCancel, T::Accepted | T::PartialFill) => Some(S::PendingCancel),
        (S::PendingCancel, T::CancelRejected) => Some(resting),
        (S::New | S::PartiallyFilled, T::Accepted | T::Rejected)
        | (S::PendingNew | S::New | S::PartiallyFilled, T::CancelRejected)
        | (S::PendingCancel, T::CancelRequested)
        | (
            S::Filled | S::Canceled | S::Rejected,
            T::Accepted
            | T::Rejected
            | T::PartialFill
            | T::FinalFill
            | T::CancelRequested
            | T::Canceled
            | T::CancelRejected,
        ) => None,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Order {
    pub request: OrderRequest,
    pub state: OrderState,
    pub venue_id: Option<VenueOrderId>,
    pub filled: Qty,
}

impl Order {
    #[must_use]
    pub fn open_qty(&self) -> Qty {
        Qty::from_raw(self.request.qty.raw() - self.filled.raw())
    }
}

/// Every order this process has sent, keyed by client ID (ordered, so replay is deterministic).
#[derive(Debug, Default)]
pub struct Oms {
    orders: BTreeMap<ClientOrderId, Order>,
}

impl Oms {
    /// Records an order about to be sent, in `PendingNew`.
    ///
    /// # Errors
    /// [`OmsError::DuplicateOrder`] if the client ID was used before.
    pub fn insert(&mut self, request: OrderRequest) -> Result<(), OmsError> {
        if self.orders.contains_key(&request.client_id) {
            return Err(OmsError::DuplicateOrder(request.client_id));
        }
        self.orders.insert(
            request.client_id,
            Order {
                request,
                state: OrderState::PendingNew,
                venue_id: None,
                filled: Qty::ZERO,
            },
        );
        Ok(())
    }

    fn step(&mut self, client_id: ClientOrderId, t: Transition) -> Result<&mut Order, OmsError> {
        let order = self
            .orders
            .get_mut(&client_id)
            .ok_or(OmsError::UnknownOrder(client_id))?;
        let next =
            transition(order.state, t, order.filled > Qty::ZERO).ok_or(OmsError::Illegal {
                client_id,
                state: order.state,
                transition: t,
            })?;
        order.state = next;
        Ok(order)
    }

    /// Marks that we asked the venue to cancel.
    ///
    /// # Errors
    /// [`OmsError`] if the order is unknown or cannot be canceled from its state.
    pub fn request_cancel(&mut self, client_id: ClientOrderId) -> Result<(), OmsError> {
        self.step(client_id, Transition::CancelRequested)
            .map(|_| ())
    }

    /// Applies a venue event and returns the order's new state. On error the order is unchanged.
    ///
    /// # Errors
    /// [`OmsError`] for unknown orders, illegal transitions and overfills.
    pub fn apply(&mut self, event: &OrderEvent) -> Result<OrderState, OmsError> {
        match *event {
            OrderEvent::Accepted {
                client_id,
                venue_id,
                ..
            } => {
                let order = self.step(client_id, Transition::Accepted)?;
                order.venue_id = Some(venue_id);
                Ok(order.state)
            }
            OrderEvent::Rejected { client_id, .. } => {
                self.step(client_id, Transition::Rejected).map(|o| o.state)
            }
            OrderEvent::Canceled { client_id, .. } => {
                self.step(client_id, Transition::Canceled).map(|o| o.state)
            }
            OrderEvent::CancelRejected { client_id, .. } => self
                .step(client_id, Transition::CancelRejected)
                .map(|o| o.state),
            OrderEvent::Filled { client_id, qty, .. } => {
                let order = self
                    .orders
                    .get(&client_id)
                    .ok_or(OmsError::UnknownOrder(client_id))?;
                let filled = order
                    .filled
                    .checked_add(qty)
                    .filter(|f| *f <= order.request.qty && qty > Qty::ZERO)
                    .ok_or(OmsError::Overfill(client_id))?;
                let t = if filled == order.request.qty {
                    Transition::FinalFill
                } else {
                    Transition::PartialFill
                };
                let order = self.step(client_id, t)?;
                order.filled = filled;
                Ok(order.state)
            }
        }
    }

    #[must_use]
    pub fn get(&self, client_id: ClientOrderId) -> Option<&Order> {
        self.orders.get(&client_id)
    }

    /// Orders that are not terminal, in client-ID order.
    pub fn open_orders(&self) -> impl Iterator<Item = &Order> {
        self.orders.values().filter(|o| !o.state.is_terminal())
    }
}

/// What the venue says about one order, from its open-orders or order-status endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct VenueOrder {
    pub venue_id: VenueOrderId,
    /// `None` when the venue has an order we did not tag, e.g. placed by hand.
    pub client_id: Option<ClientOrderId>,
    pub open: bool,
    pub filled: Qty,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Discrepancy {
    /// The venue has an open order this process never sent.
    UnknownVenueOrder(VenueOrderId),
    /// We think the order is working but the venue does not have it open.
    MissingAtVenue(ClientOrderId),
    /// The venue has it open but we think it is finished.
    OpenAtVenueOnly(ClientOrderId),
    FillMismatch {
        client_id: ClientOrderId,
        local: Qty,
        venue: Qty,
    },
}

/// Result of comparing local state with the venue. Any discrepancy means our
/// view of position or exposure is wrong, so the caller must halt trading.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Reconciliation {
    pub discrepancies: Vec<Discrepancy>,
}

impl Reconciliation {
    #[must_use]
    pub fn halt(&self) -> bool {
        !self.discrepancies.is_empty()
    }
}

/// Compares the OMS with a venue snapshot. Orders still in flight
/// (`PendingNew`, `PendingCancel`) are allowed to disagree about being open,
/// because the venue may not have processed them yet; their fills still must match.
#[must_use]
pub fn reconcile(oms: &Oms, venue: &[VenueOrder]) -> Reconciliation {
    let mut discrepancies = Vec::new();
    let mut seen = BTreeSet::new();
    for v in venue {
        let local = v.client_id.and_then(|id| oms.get(id).map(|o| (id, o)));
        let Some((id, order)) = local else {
            if v.open {
                discrepancies.push(Discrepancy::UnknownVenueOrder(v.venue_id));
            }
            continue;
        };
        seen.insert(id);
        if order.filled != v.filled {
            discrepancies.push(Discrepancy::FillMismatch {
                client_id: id,
                local: order.filled,
                venue: v.filled,
            });
        }
        let in_flight = match order.state {
            OrderState::PendingNew | OrderState::PendingCancel => true,
            OrderState::New
            | OrderState::PartiallyFilled
            | OrderState::Filled
            | OrderState::Canceled
            | OrderState::Rejected => false,
        };
        if !in_flight && v.open && order.state.is_terminal() {
            discrepancies.push(Discrepancy::OpenAtVenueOnly(id));
        }
        if !in_flight && !v.open && !order.state.is_terminal() {
            discrepancies.push(Discrepancy::MissingAtVenue(id));
        }
    }
    for order in oms.open_orders() {
        let id = order.request.client_id;
        let in_flight = matches!(
            order.state,
            OrderState::PendingNew | OrderState::PendingCancel
        );
        if !in_flight && !seen.contains(&id) {
            discrepancies.push(Discrepancy::MissingAtVenue(id));
        }
    }
    Reconciliation { discrepancies }
}

#[cfg(test)]
mod tests {
    use super::OrderState::*;
    use super::Transition as T;
    use super::*;
    use qc_core::{InstrumentId, OrderType, Price, Side, StrategyId, Timestamp};

    #[test]
    fn only_filled_canceled_rejected_are_terminal() {
        let terminal: Vec<_> = OrderState::ALL
            .into_iter()
            .filter(|s| s.is_terminal())
            .collect();
        assert_eq!(terminal, [Filled, Canceled, Rejected]);
    }

    /// The full table, written out so a change to any cell shows up here.
    #[test]
    fn every_transition_matches_the_table() {
        let n = None;
        #[rustfmt::skip]
        let table: [(OrderState, [Option<OrderState>; 7]); 7] = [
            //                Accepted          Rejected        PartialFill            FinalFill      CancelRequested      Canceled        CancelRejected
            (PendingNew,      [Some(New),       Some(Rejected), Some(PartiallyFilled), Some(Filled),  Some(PendingCancel), Some(Canceled), n]),
            (New,             [n,               n,              Some(PartiallyFilled), Some(Filled),  Some(PendingCancel), Some(Canceled), n]),
            (PartiallyFilled, [n,               n,              Some(PartiallyFilled), Some(Filled),  Some(PendingCancel), Some(Canceled), n]),
            (Filled,          [n,               n,              n,                     n,             n,                   n,              n]),
            (PendingCancel,   [Some(PendingCancel), Some(Rejected), Some(PendingCancel), Some(Filled), n,                  Some(Canceled), Some(New)]),
            (Canceled,        [n,               n,              n,                     n,             n,                   n,              n]),
            (Rejected,        [n,               n,              n,                     n,             n,                   n,              n]),
        ];
        for (state, row) in table {
            for (t, expected) in T::ALL.into_iter().zip(row) {
                assert_eq!(transition(state, t, false), expected, "{state:?} + {t:?}");
            }
        }
        assert_eq!(
            transition(PendingCancel, T::CancelRejected, true),
            Some(PartiallyFilled)
        );
        for state in OrderState::ALL.into_iter().filter(|s| s.is_terminal()) {
            for t in T::ALL {
                assert_eq!(transition(state, t, true), None);
            }
        }
    }

    fn req(id: u64, qty: i64) -> OrderRequest {
        OrderRequest {
            client_id: ClientOrderId(id),
            strategy: StrategyId(1),
            instrument: InstrumentId(1),
            side: Side::Buy,
            order_type: OrderType::Limit,
            price: Price::from_raw(100),
            qty: Qty::from_raw(qty),
        }
    }

    fn fill(id: u64, qty: i64) -> OrderEvent {
        OrderEvent::Filled {
            client_id: ClientOrderId(id),
            price: Price::from_raw(100),
            qty: Qty::from_raw(qty),
            ts: Timestamp(0),
        }
    }

    fn accept(id: u64) -> OrderEvent {
        OrderEvent::Accepted {
            client_id: ClientOrderId(id),
            venue_id: VenueOrderId(id + 100),
            ts: Timestamp(0),
        }
    }

    #[test]
    fn oms_tracks_fills_and_refuses_illegal_events_without_changing_state() {
        let mut oms = Oms::default();
        oms.insert(req(1, 10)).unwrap();
        assert_eq!(
            oms.insert(req(1, 10)),
            Err(OmsError::DuplicateOrder(ClientOrderId(1)))
        );
        assert_eq!(oms.apply(&accept(1)), Ok(New));
        assert_eq!(oms.apply(&fill(1, 4)), Ok(PartiallyFilled));
        assert_eq!(
            oms.apply(&fill(1, 7)),
            Err(OmsError::Overfill(ClientOrderId(1)))
        );
        assert_eq!(oms.get(ClientOrderId(1)).unwrap().open_qty().raw(), 6);
        oms.request_cancel(ClientOrderId(1)).unwrap();
        let cancel_rejected = OrderEvent::CancelRejected {
            client_id: ClientOrderId(1),
            ts: Timestamp(0),
        };
        assert_eq!(oms.apply(&cancel_rejected), Ok(PartiallyFilled));
        assert_eq!(oms.apply(&fill(1, 6)), Ok(Filled));
        assert_eq!(
            oms.apply(&accept(1)),
            Err(OmsError::Illegal {
                client_id: ClientOrderId(1),
                state: Filled,
                transition: T::Accepted
            })
        );
        assert_eq!(oms.get(ClientOrderId(1)).unwrap().state, Filled);
        assert_eq!(
            oms.apply(&accept(9)),
            Err(OmsError::UnknownOrder(ClientOrderId(9)))
        );
    }

    fn venue(id: u64, client: Option<u64>, open: bool, filled: i64) -> VenueOrder {
        VenueOrder {
            venue_id: VenueOrderId(id),
            client_id: client.map(ClientOrderId),
            open,
            filled: Qty::from_raw(filled),
        }
    }

    #[test]
    fn reconcile_halts_on_unknown_venue_orders_and_mismatches() {
        let mut oms = Oms::default();
        oms.insert(req(1, 10)).unwrap();
        oms.apply(&accept(1)).unwrap();
        oms.insert(req(2, 10)).unwrap(); // in flight, venue has not seen it
        oms.insert(req(3, 10)).unwrap();
        oms.apply(&accept(3)).unwrap();
        oms.apply(&fill(3, 10)).unwrap();

        let consistent = [venue(101, Some(1), true, 0), venue(103, Some(3), false, 10)];
        assert!(!reconcile(&oms, &consistent).halt());

        let unknown = [
            venue(101, Some(1), true, 0),
            venue(103, Some(3), false, 10),
            venue(555, None, true, 0),
        ];
        let r = reconcile(&oms, &unknown);
        assert!(r.halt());
        assert_eq!(
            r.discrepancies,
            [Discrepancy::UnknownVenueOrder(VenueOrderId(555))]
        );

        let r = reconcile(&oms, &[venue(103, Some(3), true, 4)]);
        assert_eq!(
            r.discrepancies,
            [
                Discrepancy::FillMismatch {
                    client_id: ClientOrderId(3),
                    local: Qty::from_raw(10),
                    venue: Qty::from_raw(4)
                },
                Discrepancy::OpenAtVenueOnly(ClientOrderId(3)),
                Discrepancy::MissingAtVenue(ClientOrderId(1)),
            ]
        );
    }
}
