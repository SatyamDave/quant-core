//! Text format for recorded market data, one record per line. The sample day
//! in `tests/replay/` and every fixture use it; the fuzz target parses it.
//!
//! ```text
//! # comment; blank lines are skipped
//! S,<instrument>,<seq>,<ts_exchange>,<ts_local>,<bids>,<asks>   snapshot; sides are `price@qty;...`, best first, may be empty
//! D,<instrument>,<seq>,<B|S>,<price>,<qty>,<ts_exchange>,<ts_local>   book delta; qty 0 removes the level
//! T,<instrument>,<B|S>,<price>,<qty>,<ts_exchange>,<ts_local>         trade; side is the aggressor
//! ```
//! Prices and quantities are fixed-point decimals, timestamps are nanoseconds.

use std::fmt;

use qc_core::{BookDelta, InstrumentId, MarketEvent, Price, Qty, Side, Timestamp, Trade};
use qc_orderbook::{BookSnapshot, MAX_LEVELS};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Record {
    Snapshot(BookSnapshot),
    Market(MarketEvent),
}

impl Record {
    #[must_use]
    pub fn ts_local(&self) -> Timestamp {
        match self {
            Record::Snapshot(s) => s.ts_local,
            Record::Market(MarketEvent::BookDelta(d)) => d.ts_local,
            Record::Market(MarketEvent::Trade(t)) => t.ts_local,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    pub line: usize,
    pub reason: &'static str,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "line {}: {}", self.line, self.reason)
    }
}

impl std::error::Error for ParseError {}

fn side(s: &str) -> Result<Side, &'static str> {
    match s {
        "B" => Ok(Side::Buy),
        "S" => Ok(Side::Sell),
        _ => Err("side must be B or S"),
    }
}

fn levels(s: &str) -> Result<Vec<(Price, Qty)>, &'static str> {
    if s.is_empty() {
        return Ok(Vec::new());
    }
    if s.split(';').count() > MAX_LEVELS {
        return Err("too many snapshot levels");
    }
    s.split(';')
        .map(|level| {
            let (p, q) = level.split_once('@').ok_or("level must be price@qty")?;
            Ok((
                p.parse().map_err(|_| "bad price")?,
                q.parse().map_err(|_| "bad qty")?,
            ))
        })
        .collect()
}

fn parse_fields(line: &str) -> Result<Option<Record>, &'static str> {
    let line = line.trim();
    if line.is_empty() || line.starts_with('#') {
        return Ok(None);
    }
    let fields: Vec<&str> = line.split(',').collect();
    let number = |s: &str| s.parse::<u64>().map_err(|_| "bad integer");
    let instrument = |s: &str| {
        s.parse::<u32>()
            .map(InstrumentId)
            .map_err(|_| "bad instrument")
    };
    let price = |s: &str| s.parse::<Price>().map_err(|_| "bad price");
    let qty = |s: &str| s.parse::<Qty>().map_err(|_| "bad qty");
    let record = match fields.as_slice() {
        ["S", inst, seq, ex, local, bids, asks] => Record::Snapshot(BookSnapshot {
            instrument: instrument(inst)?,
            seq: number(seq)?,
            bids: levels(bids)?,
            asks: levels(asks)?,
            ts_exchange: Timestamp(number(ex)?),
            ts_local: Timestamp(number(local)?),
        }),
        ["D", inst, seq, sd, px, sz, ex, local] => {
            Record::Market(MarketEvent::BookDelta(BookDelta {
                instrument: instrument(inst)?,
                seq: number(seq)?,
                side: side(sd)?,
                price: price(px)?,
                qty: qty(sz)?,
                ts_exchange: Timestamp(number(ex)?),
                ts_local: Timestamp(number(local)?),
            }))
        }
        ["T", inst, sd, px, sz, ex, local] => Record::Market(MarketEvent::Trade(Trade {
            instrument: instrument(inst)?,
            aggressor: side(sd)?,
            price: price(px)?,
            qty: qty(sz)?,
            ts_exchange: Timestamp(number(ex)?),
            ts_local: Timestamp(number(local)?),
        })),
        _ => return Err("unknown record kind or wrong field count"),
    };
    Ok(Some(record))
}

/// Parses one line; `Ok(None)` for blank lines and comments.
///
/// # Errors
/// [`ParseError`] naming the line and what was wrong with it.
pub fn parse_line(line_no: usize, line: &str) -> Result<Option<Record>, ParseError> {
    parse_fields(line).map_err(|reason| ParseError {
        line: line_no,
        reason,
    })
}

/// Parses a whole recording.
///
/// # Errors
/// The first [`ParseError`].
pub fn parse(text: &str) -> Result<Vec<Record>, ParseError> {
    text.lines()
        .enumerate()
        .filter_map(|(i, l)| parse_line(i + 1, l).transpose())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_each_kind_and_rejects_garbage() {
        let text = "# day\n\nS,1,10,5,6,100@1;99@2,101@3\nD,1,11,B,100,0,7,8\nT,1,S,99,0.5,9,10\n";
        let records = parse(text).unwrap();
        assert_eq!(records.len(), 3);
        let Record::Snapshot(s) = &records[0] else {
            panic!("not a snapshot")
        };
        assert_eq!((s.seq, s.bids.len(), s.asks.len()), (10, 2, 1));
        assert_eq!(records[2].ts_local(), Timestamp(10));
        for bad in [
            "X,1",
            "D,1,11,Q,100,0,7,8",
            "D,1,11,B,1e3,0,7,8",
            "T,1,S,99,0.5,9",
            "S,1,1,1,1,100,",
            "D,-1,11,B,100,0,7,8",
        ] {
            assert!(parse_line(1, bad).is_err(), "{bad}");
        }
        assert_eq!(parse_line(3, "S,1,1,1,1,1@1;x,").unwrap_err().line, 3);
    }
}
