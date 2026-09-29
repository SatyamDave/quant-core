// Same three cases as data/quality/tests/test_quality.py's check_deltas tests (clean data
// passes; gap+duplicate+skew located by index; excess latency alone is skew) -- this recorder
// reimplements the same checks in TS (quality.ts's header explains why), and this test proves
// it locates the same things at the same indices.
import { describe, expect, it } from "vitest";
import { checkQuality } from "../../src/recorder/quality.js";
import type { SnapshotRecord } from "../../src/recorder/record-writer.js";

function records(seq: number[], exchange: number[], local: number[]): SnapshotRecord[] {
  return seq.map((s, i) => ({
    instrument: 1,
    seq: s,
    tsExchangeNs: BigInt(exchange[i]!),
    tsLocalNs: BigInt(local[i]!),
    bids: [{ price: "100.00", qty: "1" }],
    asks: [{ price: "100.05", qty: "1" }],
  }));
}

describe("checkQuality", () => {
  it("passes clean, strictly-increasing data", () => {
    const report = checkQuality(records([1, 2, 3], [10, 20, 30], [15, 25, 35]), 100n, 1_000n);
    expect(report.ok).toBe(true);
    expect(report.timeGaps).toEqual([]);
    expect(report.seqGaps).toEqual([]);
    expect(report.duplicateSeqs).toEqual([]);
    expect(report.clockSkew).toEqual([]);
  });

  it("locates a gap, a duplicate, and clock skew by index", () => {
    const report = checkQuality(records([1, 2, 2, 5, 6], [10, 20, 30, 40, 200], [15, 25, 29, 45, 205]), 100n, 1_000_000n);
    expect(report.duplicateSeqs).toEqual([2]);
    expect(report.seqGaps).toEqual([3]);
    expect(report.clockSkew).toEqual([2]); // local_ts (29) before exchange_ts (30)
    expect(report.timeGaps).toEqual([4]); // exchange gap 200-40=160 > maxGapNs 100
    expect(report.ok).toBe(false);
  });

  it("flags excess latency (local well after exchange) as skew even with no gap/duplicate", () => {
    const report = checkQuality(records([1, 2], [10, 20], [15, 20 + 2_000]), 1_000_000n, 1_000n);
    expect(report.clockSkew).toEqual([1]);
    expect(report.seqGaps).toEqual([]);
    expect(report.duplicateSeqs).toEqual([]);
  });

  it("is ok on a single record or no records", () => {
    expect(checkQuality([], 100n, 100n).ok).toBe(true);
    expect(checkQuality(records([1], [10], [15]), 100n, 100n).ok).toBe(true);
  });
});
