//! The contract between the engine and a strategy, plus one example strategy.

use qc_core::{
    ClientOrderId, Event, InstrumentId, MarketEvent, OrderEvent, OrderRequest, OrderType, Price,
    Qty, Side, StrategyId,
};
use qc_orderbook::{BookStatus, OrderBook};

/// What a strategy asks the engine to do. Submits pass through risk; cancels
/// always go out, because canceling only ever reduces exposure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Command {
    Submit(OrderRequest),
    Cancel(ClientOrderId),
}

/// A strategy turns events into commands. It must be deterministic: the same
/// events in the same order produce the same commands. `book` is the engine's
/// book after the event was applied. It pushes into `out` rather than
/// returning a `Vec` so the hot path does not allocate. Orders refused by risk
/// or the gateway come back as [`OrderEvent::Rejected`].
pub trait Strategy {
    fn on_event(&mut self, event: &Event, book: &OrderBook, out: &mut Vec<Command>);
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Quote {
    id: ClientOrderId,
    price: Price,
    open: Qty,
    canceling: bool,
}

/// Example market maker: joins the best bid and best ask with post-only
/// orders of `qty`, requotes when the inside moves, stops quoting a side once
/// its inventory reaches `max_inventory`, and pulls both quotes whenever the
/// book is out of sync. It acts only on market data. It is a test fixture for the engine, not a strategy
/// that has passed any gate.
#[derive(Debug)]
pub struct InsideQuoter {
    strategy: StrategyId,
    instrument: InstrumentId,
    qty: Qty,
    max_inventory: Qty,
    next_id: u64,
    position: Qty,
    bid: Option<Quote>,
    ask: Option<Quote>,
}

impl InsideQuoter {
    #[must_use]
    pub fn new(
        strategy: StrategyId,
        instrument: InstrumentId,
        qty: Qty,
        max_inventory: Qty,
    ) -> Self {
        Self {
            strategy,
            instrument,
            qty,
            max_inventory,
            next_id: 0,
            position: Qty::ZERO,
            bid: None,
            ask: None,
        }
    }

    #[must_use]
    pub fn position(&self) -> Qty {
        self.position
    }

    fn slot(&mut self, side: Side) -> &mut Option<Quote> {
        match side {
            Side::Buy => &mut self.bid,
            Side::Sell => &mut self.ask,
        }
    }

    fn on_order(&mut self, event: &OrderEvent) {
        let id = match *event {
            OrderEvent::Accepted { client_id, .. }
            | OrderEvent::Rejected { client_id, .. }
            | OrderEvent::Filled { client_id, .. }
            | OrderEvent::Canceled { client_id, .. }
            | OrderEvent::CancelRejected { client_id, .. } => client_id,
        };
        let side = match (self.bid, self.ask) {
            (Some(q), _) if q.id == id => Side::Buy,
            (_, Some(q)) if q.id == id => Side::Sell,
            _ => return,
        };
        let slot = self.slot(side);
        let Some(mut quote) = *slot else { return };
        match *event {
            OrderEvent::Accepted { .. } => {}
            OrderEvent::Rejected { .. } | OrderEvent::Canceled { .. } => *slot = None,
            OrderEvent::CancelRejected { .. } => {
                quote.canceling = false;
                *slot = (quote.open > Qty::ZERO).then_some(quote);
            }
            OrderEvent::Filled { qty, .. } => {
                quote.open = Qty::from_raw(quote.open.raw() - qty.raw());
                *slot = (quote.open > Qty::ZERO).then_some(quote);
                let signed = match side {
                    Side::Buy => qty.raw(),
                    Side::Sell => -qty.raw(),
                };
                self.position = Qty::from_raw(self.position.raw() + signed);
            }
        }
    }

    fn requote(&mut self, side: Side, target: Option<Price>, out: &mut Vec<Command>) {
        let inventory = match side {
            Side::Buy => self.position.raw(),
            Side::Sell => -self.position.raw(),
        };
        let target = target.filter(|_| inventory < self.max_inventory.raw());
        let (strategy, instrument, qty) = (self.strategy, self.instrument, self.qty);
        let slot = match side {
            Side::Buy => &mut self.bid,
            Side::Sell => &mut self.ask,
        };
        match (slot.as_mut(), target) {
            (Some(q), t) if !q.canceling && t != Some(q.price) => {
                q.canceling = true;
                out.push(Command::Cancel(q.id));
            }
            (Some(_), _) | (None, None) => {}
            (None, Some(price)) => {
                self.next_id += 1;
                let id = ClientOrderId(self.next_id);
                *slot = Some(Quote {
                    id,
                    price,
                    open: qty,
                    canceling: false,
                });
                out.push(Command::Submit(OrderRequest {
                    client_id: id,
                    strategy,
                    instrument,
                    side,
                    order_type: OrderType::PostOnly,
                    price,
                    qty,
                }));
            }
        }
    }
}

impl Strategy for InsideQuoter {
    fn on_event(&mut self, event: &Event, book: &OrderBook, out: &mut Vec<Command>) {
        // Quotes change only on market data. Requoting straight from an order
        // event would resubmit at once after a risk reject, in a loop.
        match event {
            Event::Order(o) => return self.on_order(o),
            Event::Market(MarketEvent::BookDelta(_) | MarketEvent::Trade(_)) => {}
        }
        let synced = book.status() == BookStatus::Synced && book.instrument() == self.instrument;
        let (bid, ask) = if synced {
            (book.best_bid().map(|l| l.0), book.best_ask().map(|l| l.0))
        } else {
            (None, None)
        };
        self.requote(Side::Buy, bid, out);
        self.requote(Side::Sell, ask, out);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use qc_core::{BookDelta, Timestamp};
    use qc_orderbook::BookSnapshot;

    fn book(bid: i64, ask: i64) -> OrderBook {
        let mut b = OrderBook::new(InstrumentId(1));
        b.apply_snapshot(&BookSnapshot {
            instrument: InstrumentId(1),
            seq: 1,
            bids: vec![(Price::from_raw(bid), Qty::from_raw(5))],
            asks: vec![(Price::from_raw(ask), Qty::from_raw(5))],
            ts_exchange: Timestamp(1),
            ts_local: Timestamp(1),
        })
        .unwrap();
        b
    }

    fn delta(seq: u64) -> BookDelta {
        BookDelta {
            instrument: InstrumentId(1),
            side: Side::Buy,
            price: Price::from_raw(1),
            qty: Qty::ZERO,
            seq,
            ts_exchange: Timestamp(seq),
            ts_local: Timestamp(seq),
        }
    }

    fn tick() -> Event {
        Event::Market(MarketEvent::BookDelta(delta(2)))
    }

    fn submitted(out: &[Command]) -> Vec<(Side, i64)> {
        out.iter()
            .filter_map(|c| match c {
                Command::Submit(o) => Some((o.side, o.price.raw())),
                Command::Cancel(_) => None,
            })
            .collect()
    }

    #[test]
    fn quotes_the_inside_requotes_on_moves_and_respects_inventory() {
        let mut s = InsideQuoter::new(
            StrategyId(1),
            InstrumentId(1),
            Qty::from_raw(1),
            Qty::from_raw(1),
        );
        let mut out = Vec::new();
        s.on_event(&tick(), &book(100, 102), &mut out);
        assert_eq!(submitted(&out), [(Side::Buy, 100), (Side::Sell, 102)]);

        out.clear();
        s.on_event(&tick(), &book(101, 102), &mut out);
        assert_eq!(out, [Command::Cancel(ClientOrderId(1))]);
        out.clear();
        let canceled = Event::Order(OrderEvent::Canceled {
            client_id: ClientOrderId(1),
            ts: Timestamp(3),
        });
        s.on_event(&canceled, &book(101, 102), &mut out);
        assert!(out.is_empty(), "waits for market data before requoting");
        s.on_event(&tick(), &book(101, 102), &mut out);
        assert_eq!(submitted(&out), [(Side::Buy, 101)]);

        // The new bid fills; long inventory is at the cap, so no new bid.
        out.clear();
        let fill = Event::Order(OrderEvent::Filled {
            client_id: ClientOrderId(3),
            price: Price::from_raw(101),
            qty: Qty::from_raw(1),
            ts: Timestamp(4),
        });
        s.on_event(&fill, &book(101, 102), &mut out);
        s.on_event(&tick(), &book(101, 102), &mut out);
        assert!(out.is_empty());
        assert_eq!(s.position(), Qty::from_raw(1));

        // Out-of-sync book: pull the remaining ask.
        let mut broken = book(101, 102);
        broken.apply(&delta(9)).unwrap_err();
        s.on_event(&tick(), &broken, &mut out);
        assert_eq!(out, [Command::Cancel(ClientOrderId(2))]);
    }
}
