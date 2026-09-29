// Unit-level proof of issue #46's "the recorder can only read prices": readOnly() hands back an
// object with only getQuote/getBook, so no order method is reachable through it at all.
import { describe, expect, it } from "vitest";
import { READ_ONLY_METHODS, readOnly } from "../../src/recorder/allowlist.js";
import { pollBook, pollQuote } from "../../src/recorder/poll.js";
import { MockBroker } from "../../src/testkit/mock-broker.js";

describe("readOnly()", () => {
  it("exposes only the read-only market-data methods", () => {
    expect(Object.keys(readOnly(new MockBroker())).sort()).toEqual([...READ_ONLY_METHODS].sort());
  });

  it("never reaches an order method: the wrapped object has none to call", () => {
    const wrapped = readOnly(new MockBroker()) as unknown as Record<string, unknown>;
    for (const method of ["placeOrder", "cancelOrder", "previewOrder", "getOrderById", "findOrderByRefId", "listOrders", "getAccountState", "verifyAccount"]) {
      expect(wrapped[method]).toBeUndefined();
    }
  });

  it("polls a book and a quote through the wrapper, and the broker sees only those calls", async () => {
    const broker = new MockBroker();
    const client = readOnly(broker);
    const book = await pollBook(client, "equity", "SPY");
    expect(book.depth).toBe("l2");
    const quote = await pollQuote(client, "equity", "SPY");
    expect([quote.bid, quote.ask]).toEqual(["100.00", "100.05"]);
    expect(broker.calls.map((c) => c.method)).toEqual(["getBook", "getQuote"]);
  });

  it("falls back to a flagged top-of-book sample when the book has an empty side", async () => {
    const sample = await pollBook(readOnly(new MockBroker({ emptyBook: true })), "equity", "SPY");
    expect(sample.depth).toBe("top_of_book");
    expect(sample.bids).toEqual([{ price: "100.00" }]);
  });

  it("falls back to top-of-book for an asset class with no L2 book", async () => {
    const sample = await pollBook(readOnly(new MockBroker()), "crypto", "BTC-USD");
    expect(sample.depth).toBe("top_of_book");
  });
});
