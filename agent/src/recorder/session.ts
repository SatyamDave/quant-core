// The poll loop: pulls one book every pollIntervalMs, up to requestBudget requests total, and
// writes each as a Snapshot record. requestBudget is the recorder's own conservative cap, not
// a venue's measured rate limit: set it from your broker's documented limits.
import type { MarketData } from "../broker/adapter.js";
import type { AssetClass, DecimalString } from "../broker/types.js";
import { pollBook } from "./poll.js";
import { toSnapshotRecord, type RecordWriter } from "./record-writer.js";

export interface SessionOptions {
  client: MarketData;
  assetClass: AssetClass;
  symbol: string;
  instrumentId: number;
  pollIntervalMs: number;
  requestBudget: number;
  placeholderQty: DecimalString;
  writer: RecordWriter;
  /** Injectable for tests; defaults to a real timer. */
  sleep?: (ms: number) => Promise<void>;
  /** Set (e.g. by a SIGINT/SIGTERM handler) to stop early and still finalize cleanly. */
  signal?: AbortSignal;
}

export interface SessionResult {
  requestsMade: number;
  /** How many polls got real Level 2 depth vs. fell back to a top-of-book quote (BookSample's
   *  own `depth` flag, tallied) -- the catalog manifest's own record of that flag, so a run that
   *  silently degraded to top-of-book for its whole session is visible after the fact, not just
   *  in a transient in-process value. */
  depthCounts: { l2: number; top_of_book: number };
}

const defaultSleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Runs the poll loop to completion (budget exhausted, or `signal` aborted between polls). */
export async function runSession(opts: SessionOptions): Promise<SessionResult> {
  const sleep = opts.sleep ?? defaultSleep;
  let seq = 0;
  let requestsMade = 0;
  const depthCounts = { l2: 0, top_of_book: 0 };
  while (requestsMade < opts.requestBudget) {
    if (opts.signal?.aborted) break;
    requestsMade++;
    const sample = await pollBook(opts.client, opts.assetClass, opts.symbol);
    depthCounts[sample.depth]++;
    seq++;
    opts.writer.writeSnapshot(toSnapshotRecord(sample, opts.instrumentId, seq, opts.placeholderQty));
    if (requestsMade >= opts.requestBudget || opts.signal?.aborted) break;
    await sleep(opts.pollIntervalMs);
  }
  return { requestsMade, depthCounts };
}
