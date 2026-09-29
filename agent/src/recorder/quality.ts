// Data-quality checks run on recorder output, mirroring data/quality/__init__.py's
// check_deltas (data/quality/README.md: "sequence gaps, duplicate or backwards sequence numbers,
// exchange-time gaps, and clock skew"). Reimplemented here, not imported, because this lane owns
// only agent/src/recorder/** (wave2-spec); data/quality/** is Python and a different lane's/
// module's surface. Same four checks, same "offending row indices" shape.
import type { SnapshotRecord } from "./record-writer.js";

export interface QualityReport {
  ok: boolean;
  /** Indices where seq skipped ahead by more than 1 from the previous record. */
  seqGaps: number[];
  /** Indices where seq did not strictly increase from the previous record. */
  duplicateSeqs: number[];
  /** Indices where the exchange-time gap from the previous record exceeded maxGapNs. */
  timeGaps: number[];
  /** Indices where local receipt time preceded exchange time, or exceeded maxLatencyNs after it. */
  clockSkew: number[];
}

/** `maxGapNs`/`maxLatencyNs` are required, not defaulted here: what counts as a gap is relative
 *  to the configured poll interval, and the recorder CLI (cli.ts) is the only place that knows
 *  it. */
export function checkQuality(records: readonly SnapshotRecord[], maxGapNs: bigint, maxLatencyNs: bigint): QualityReport {
  const seqGaps: number[] = [];
  const duplicateSeqs: number[] = [];
  const timeGaps: number[] = [];
  const clockSkew: number[] = [];

  for (let i = 1; i < records.length; i++) {
    const prev = records[i - 1]!;
    const cur = records[i]!;
    if (cur.seq <= prev.seq) {
      duplicateSeqs.push(i);
    } else if (cur.seq - prev.seq > 1) {
      seqGaps.push(i);
    }
    if (cur.tsExchangeNs - prev.tsExchangeNs > maxGapNs) {
      timeGaps.push(i);
    }
  }

  for (let i = 0; i < records.length; i++) {
    const r = records[i]!;
    const latencyNs = r.tsLocalNs - r.tsExchangeNs;
    if (latencyNs < 0n || latencyNs > maxLatencyNs) {
      clockSkew.push(i);
    }
  }

  return {
    ok: seqGaps.length === 0 && duplicateSeqs.length === 0 && timeGaps.length === 0 && clockSkew.length === 0,
    seqGaps,
    duplicateSeqs,
    timeGaps,
    clockSkew,
  };
}
