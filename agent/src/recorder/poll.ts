// Reads market data through the broker adapter's read-only half (adapter.ts's MarketData, handed
// in already narrowed by allowlist.ts's readOnly()).
//
// pollBook() is the recorder's main entry point: real Level 2 depth via getBook() when the venue
// has it for the asset class, falling back to pollQuote()'s single top-of-book level when it
// doesn't. The fallback is flagged in BookSample.depth, never silent (root CLAUDE.md: no silent
// fallbacks). Two things trigger it: adapter.ts's BookUnsupportedError (a documented structural
// gap, not an ambiguous/transport failure), and a book with an empty side (venues often return
// empty levels after hours). Any other error from getBook still propagates.
import { BookUnsupportedError, type MarketData } from "../broker/adapter.js";
import type { AssetClass, BookLevel, DecimalString } from "../broker/types.js";

export interface QuoteSample {
  bid: DecimalString;
  ask: DecimalString;
  /** Nanoseconds since epoch, millisecond precision (Date.now() * 1e6).
   *  ponytail: exchange timestamp is copied from localTsNs: the adapter's Quote has no exchange
   *  timestamp field, so clock-skew checks are always zero for it today. Add one to Quote and
   *  read it here once an adapter can supply it. */
  exchangeTsNs: bigint;
  localTsNs: bigint;
  raw: Record<string, unknown>;
}

export function nowNs(): bigint {
  return BigInt(Date.now()) * 1_000_000n;
}

/** Polls one quote for `instrument` and returns the sample. Throws if the response doesn't
 *  parse -- same "ambiguous response is an error, never a guess" convention as adapter.ts. */
export async function pollQuote(client: MarketData, assetClass: AssetClass, instrument: string): Promise<QuoteSample> {
  const localTsNs = nowNs();
  const { bid, ask, raw } = await client.getQuote(assetClass, instrument);
  return { bid, ask, exchangeTsNs: localTsNs, localTsNs, raw };
}

/** One book level as pollBook() reports it. `qty` is present with a real resting size for an
 *  `"l2"` sample; absent for a `"top_of_book"` fallback sample, where the caller (record-writer's
 *  toSnapshotRecord) fills in the recorder's own configured placeholder -- the same convention
 *  pollQuote's single-level snapshot has always used. */
export interface PolledLevel {
  price: DecimalString;
  qty?: DecimalString;
}

export interface BookSample {
  bids: PolledLevel[];
  asks: PolledLevel[];
  /** "l2": real per-level resting size from getBook(). "top_of_book": no L2 data
   *  for this asset class, or the real book had an empty side -- one level per side, price
   *  only, built from pollQuote(). */
  depth: "l2" | "top_of_book";
  exchangeTsNs: bigint;
  localTsNs: bigint;
  raw: Record<string, unknown>;
}

/** Mirrors qc_orderbook::MAX_LEVELS (engine/crates/orderbook/src/lib.rs). */
export const MAX_BOOK_LEVELS = 256;

/** Polls the full order book for `instrument` when the venue publishes one for this asset class,
 *  or falls back to a single top-of-book level built from the quote when it doesn't. See
 *  the module header for which errors trigger the fallback and which still propagate. */
export async function pollBook(client: MarketData, assetClass: AssetClass, instrument: string): Promise<BookSample> {
  const localTsNs = nowNs();
  try {
    const book = await client.getBook(assetClass, instrument);
    if (book.bids.length > 0 && book.asks.length > 0) {
      // A real book can carry thousands of levels; qc-bridge rejects a snapshot with more than
      // qc_orderbook::MAX_LEVELS per side, so keep the best levels (they arrive best-first).
      const bids = book.bids.slice(0, MAX_BOOK_LEVELS);
      const asks = book.asks.slice(0, MAX_BOOK_LEVELS);
      return { bids, asks, depth: "l2", exchangeTsNs: localTsNs, localTsNs, raw: book.raw };
    }
  } catch (err) {
    if (!(err instanceof BookUnsupportedError)) throw err;
  }
  const quote = await pollQuote(client, assetClass, instrument);
  return {
    bids: [{ price: quote.bid }],
    asks: [{ price: quote.ask }],
    depth: "top_of_book",
    exchangeTsNs: quote.exchangeTsNs,
    localTsNs: quote.localTsNs,
    raw: quote.raw,
  };
}

// Re-exported so callers only need one import for "a book level, real or placeholder-pending".
export type { BookLevel };
