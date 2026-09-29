// Proves the poll loop respects both configurable knobs: it stops at exactly requestBudget
// calls (never more, even if aborted late), and it sleeps pollIntervalMs between polls (never
// after the last one). Uses a fake market-data source and an injected sleep spy -- no real timers, no
// network.
import { describe, expect, it, vi } from "vitest";
import type { MarketData } from "../../src/broker/adapter.js";
import { runSession } from "../../src/recorder/session.js";
import { RecordWriter } from "../../src/recorder/record-writer.js";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

function bookClient(): MarketData & { getBook: ReturnType<typeof vi.fn> } {
  return {
    getQuote: vi.fn(),
    getBook: vi.fn().mockResolvedValue({
      bids: [{ price: "99.99", qty: "500" }],
      asks: [{ price: "100.05", qty: "400" }],
      raw: { source: "SYNTHETIC" },
    }),
  };
}

describe("runSession", () => {
  it("stops at exactly requestBudget polls and sleeps pollIntervalMs between each (never after the last)", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "recorder-session-"));
    try {
      const writer = new RecordWriter(path.join(dir, "out.csv"));
      const client = bookClient();
      const sleep = vi.fn().mockResolvedValue(undefined);

      const result = await runSession({
        client,
        assetClass: "equity",
        symbol: "SPY",
        instrumentId: 1,
        pollIntervalMs: 250,
        requestBudget: 4,
        placeholderQty: "1",
        writer,
        sleep,
      });
      writer.close();

      expect(result.requestsMade).toBe(4);
      expect(client.getBook).toHaveBeenCalledTimes(4);
      // 3 sleeps between 4 polls, never a trailing sleep after the budget is exhausted.
      expect(sleep).toHaveBeenCalledTimes(3);
      expect(sleep).toHaveBeenCalledWith(250);
      expect(writer.written.map((r) => r.seq)).toEqual([1, 2, 3, 4]);
      // Every poll got a real book response -- all 4 counted as "l2", none fell back.
      expect(result.depthCounts).toEqual({ l2: 4, top_of_book: 0 });
      expect(writer.written[0]!.bids).toEqual([{ price: "99.99", qty: "500" }]);
      expect(writer.written[0]!.asks).toEqual([{ price: "100.05", qty: "400" }]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("stops early when the abort signal fires between polls, without exceeding the budget", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "recorder-session-"));
    try {
      const writer = new RecordWriter(path.join(dir, "out.csv"));
      const client = bookClient();
      const controller = new AbortController();
      let calls = 0;
      const sleep = vi.fn().mockImplementation(async () => {
        calls++;
        if (calls === 2) controller.abort();
      });

      const result = await runSession({
        client,
        assetClass: "equity",
        symbol: "SPY",
        instrumentId: 1,
        pollIntervalMs: 1,
        requestBudget: 100,
        placeholderQty: "1",
        writer,
        sleep,
        signal: controller.signal,
      });
      writer.close();

      // Poll 1 -> sleep (call 1, no abort yet) -> poll 2 -> sleep (call 2, aborts) -> loop
      // re-checks the signal before a third poll and stops.
      expect(result.requestsMade).toBe(2);
      expect(result.requestsMade).toBeLessThan(100);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
