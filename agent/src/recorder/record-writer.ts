// Writes recorder output in exactly the record format engine/crates/gateway/src/record.rs
// parses (the same format as tests/replay/sample_day.csv):
//   S,<instrument>,<seq>,<ts_exchange>,<ts_local>,<bids>,<asks>   snapshot
// `bids`/`asks` are `price@qty;price@qty...`, best level first. Prices/quantities are the fixed-
// point decimal strings qc_core::Price/Qty::from_str accepts (plain decimal, <=8 fraction
// digits, no exponent -- never a float). Every write is fsync'd before the next one, same
// durability convention as agent/src/broker/journal.ts: a crash mid-recording must never lose an
// already-appended line.
//
// Only Snapshot ("S") records are produced. Delta ("D") and Trade ("T") lines are real,
// documented parts of the format (record.rs), but this recorder has no source for them: book
// polling (poll.ts's pollBook) gives a fresh snapshot each call, not an incremental book delta or
// a trade tape, and fabricating either from a poll would be recording data that was never
// actually observed. Emitting only what was genuinely seen matters more than a full record mix;
// a future delta/trade feed source is what would add D/T lines.
import { closeSync, fsyncSync, mkdirSync, openSync, writeSync } from "node:fs";
import path from "node:path";
import type { DecimalString } from "../broker/types.js";
import type { BookSample } from "./poll.js";

export interface BookLevelRecord {
  price: DecimalString;
  qty: DecimalString;
}

export interface SnapshotRecord {
  instrument: number;
  seq: number;
  tsExchangeNs: bigint;
  tsLocalNs: bigint;
  /** Best level first, per record.rs's grammar -- trusted to arrive that way from
   *  BookSample (see poll.ts/adapter.ts's own "no re-sorting, ambiguous is an error"
   *  convention), never reordered here. */
  bids: BookLevelRecord[];
  asks: BookLevelRecord[];
}

function fillPlaceholderQty(levels: BookSample["bids"], placeholderQty: DecimalString): BookLevelRecord[] {
  return levels.map((l) => ({ price: l.price, qty: l.qty ?? placeholderQty }));
}

/** Builds the SnapshotRecord for one poll. `placeholderQty` fills any level with no real size --
 *  every level of a `"top_of_book"` BookSample (a quote carries no book
 *  depth; poll.ts's header), never a level of an `"l2"` sample, which always carries the venue's
 *  own resting size. `seq` must be caller-assigned and strictly increasing per instrument. */
export function toSnapshotRecord(sample: BookSample, instrument: number, seq: number, placeholderQty: DecimalString): SnapshotRecord {
  return {
    instrument,
    seq,
    tsExchangeNs: sample.exchangeTsNs,
    tsLocalNs: sample.localTsNs,
    bids: fillPlaceholderQty(sample.bids, placeholderQty),
    asks: fillPlaceholderQty(sample.asks, placeholderQty),
  };
}

function formatSide(levels: readonly BookLevelRecord[]): string {
  return levels.map((l) => `${l.price}@${l.qty}`).join(";");
}

export function formatSnapshot(r: SnapshotRecord): string {
  return `S,${r.instrument},${r.seq},${r.tsExchangeNs},${r.tsLocalNs},${formatSide(r.bids)},${formatSide(r.asks)}`;
}

/** Append-only writer for one recording session. Not thread-safe / not for concurrent writers --
 *  one recorder process owns one output file. */
export class RecordWriter {
  private readonly fd: number;
  private readonly records: SnapshotRecord[] = [];

  constructor(
    public readonly filePath: string,
    headerLines: readonly string[] = [],
  ) {
    mkdirSync(path.dirname(filePath), { recursive: true });
    this.fd = openSync(filePath, "w");
    for (const line of headerLines) {
      writeSync(this.fd, `# ${line}\n`);
    }
    fsyncSync(this.fd);
  }

  writeSnapshot(record: SnapshotRecord): void {
    writeSync(this.fd, `${formatSnapshot(record)}\n`);
    fsyncSync(this.fd);
    this.records.push(record);
  }

  /** Every snapshot written so far, in write order -- what quality.ts and catalog.ts run over. */
  get written(): readonly SnapshotRecord[] {
    return this.records;
  }

  close(): void {
    closeSync(this.fd);
  }
}
