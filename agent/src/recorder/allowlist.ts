// Issue #46: "the recorder can only read prices -- it holds no handle that could place a trade."
// readOnly() narrows a full BrokerAdapter to its market-data half at runtime, so the object the
// recorder holds has no place/cancel/preview method to call at all.
// agent/tests/recorder/allowlist.test.ts fails if an order method is ever reachable through it.
import type { BrokerAdapter, MarketData } from "../broker/adapter.js";

export const READ_ONLY_METHODS: ReadonlySet<keyof MarketData> = new Set(["getQuote", "getBook"]);

/** A new object exposing only getQuote/getBook, bound to `broker`. */
export function readOnly(broker: BrokerAdapter | MarketData): MarketData {
  return {
    getQuote: (...args) => broker.getQuote(...args),
    getBook: (...args) => broker.getBook(...args),
  };
}
