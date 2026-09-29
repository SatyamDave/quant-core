// Proves the exact text this recorder writes matches engine/crates/gateway/src/record.rs's
// grammar byte for byte: `S,<instrument>,<seq>,<ts_exchange>,<ts_local>,<bids>,<asks>`, sides as
// `price@qty;price@qty...`, best level first. The real proof that a whole file parses is
// agent/tests/recorder/replay-integration.test.ts (runs it through the actual qc-replay binary);
// this test pins the per-line format so a regression here is caught without a Rust build.
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { formatSnapshot, RecordWriter, toSnapshotRecord, type SnapshotRecord } from "../../src/recorder/record-writer.js";
import type { BookSample } from "../../src/recorder/poll.js";

describe("formatSnapshot", () => {
  it("matches record.rs's S,<instrument>,<seq>,<ts_exchange>,<ts_local>,<bids>,<asks> grammar for a single level per side", () => {
    const record: SnapshotRecord = {
      instrument: 1,
      seq: 42,
      tsExchangeNs: 1767571200266000000n,
      tsLocalNs: 1767571200266700396n,
      bids: [{ price: "65000.10000000", qty: "1.00000000" }],
      asks: [{ price: "65000.20000000", qty: "1.00000000" }],
    };
    expect(formatSnapshot(record)).toBe(
      "S,1,42,1767571200266000000,1767571200266700396,65000.10000000@1.00000000,65000.20000000@1.00000000",
    );
  });

  it("joins multiple levels per side with ';', best level first -- real order-book depth", () => {
    const record: SnapshotRecord = {
      instrument: 1,
      seq: 1,
      tsExchangeNs: 10n,
      tsLocalNs: 11n,
      bids: [
        { price: "99.99", qty: "500" },
        { price: "99.98", qty: "300" },
      ],
      asks: [
        { price: "100.05", qty: "400" },
        { price: "100.06", qty: "250" },
      ],
    };
    expect(formatSnapshot(record)).toBe("S,1,1,10,11,99.99@500;99.98@300,100.05@400;100.06@250");
  });
});

describe("toSnapshotRecord", () => {
  it("carries an l2 sample's real per-level sizes through unchanged (placeholder unused)", () => {
    const sample: BookSample = {
      bids: [
        { price: "99.99", qty: "500" },
        { price: "99.98", qty: "300" },
      ],
      asks: [{ price: "100.05", qty: "400" }],
      depth: "l2",
      exchangeTsNs: 10n,
      localTsNs: 15n,
      raw: {},
    };
    const record = toSnapshotRecord(sample, 7, 3, "0.50000000");
    expect(record).toEqual({
      instrument: 7,
      seq: 3,
      tsExchangeNs: 10n,
      tsLocalNs: 15n,
      bids: [
        { price: "99.99", qty: "500" },
        { price: "99.98", qty: "300" },
      ],
      asks: [{ price: "100.05", qty: "400" }],
    });
  });

  it("fills the placeholder qty for a top_of_book fallback sample, which has no real size", () => {
    const sample: BookSample = {
      bids: [{ price: "100.00" }],
      asks: [{ price: "100.05" }],
      depth: "top_of_book",
      exchangeTsNs: 10n,
      localTsNs: 15n,
      raw: {},
    };
    const record = toSnapshotRecord(sample, 7, 3, "0.50000000");
    expect(record).toEqual({
      instrument: 7,
      seq: 3,
      tsExchangeNs: 10n,
      tsLocalNs: 15n,
      bids: [{ price: "100.00", qty: "0.50000000" }],
      asks: [{ price: "100.05", qty: "0.50000000" }],
    });
  });
});

describe("RecordWriter", () => {
  let dir: string | undefined;
  afterEach(() => {
    if (dir) rmSync(dir, { recursive: true, force: true });
    dir = undefined;
  });

  it("writes a comment header (skipped by record.rs's parser) then one S line per snapshot", () => {
    dir = mkdtempSync(path.join(tmpdir(), "recorder-writer-"));
    const file = path.join(dir, "out.csv");
    const writer = new RecordWriter(file, ["a header line", "another"]);
    writer.writeSnapshot({ instrument: 1, seq: 1, tsExchangeNs: 10n, tsLocalNs: 11n, bids: [{ price: "1.0", qty: "1" }], asks: [{ price: "1.1", qty: "1" }] });
    writer.writeSnapshot({ instrument: 1, seq: 2, tsExchangeNs: 20n, tsLocalNs: 21n, bids: [{ price: "1.0", qty: "1" }], asks: [{ price: "1.1", qty: "1" }] });
    writer.close();

    const lines = readFileSync(file, "utf8").trimEnd().split("\n");
    expect(lines[0]).toBe("# a header line");
    expect(lines[1]).toBe("# another");
    expect(lines[2]).toBe("S,1,1,10,11,1.0@1,1.1@1");
    expect(lines[3]).toBe("S,1,2,20,21,1.0@1,1.1@1");
    expect(writer.written).toHaveLength(2);
  });
});
