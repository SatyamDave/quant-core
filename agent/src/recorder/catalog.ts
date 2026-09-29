// Writes one data/catalog/<dataset>.json manifest per recording session, following the existing
// convention (data/catalog/study-0001.json, study-0002.json): "what exists, where it lives, and
// its schema version -- never the data itself" (data/catalog/README.md). The recorded CSV itself
// stays under data/raw/ (gitignored); only this manifest and its content hash are committed.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import type { AssetClass } from "../broker/types.js";
import type { QualityReport } from "./quality.js";
import type { SnapshotRecord } from "./record-writer.js";

export function sha256File(filePath: string): string {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

export interface CatalogManifest {
  dataset: string;
  schema_version: "recorder-catalog-v1";
  catalog_written_at: string;
  record_format: string;
  source: {
    /** "mock-broker" for --dry-run, "broker-adapter" for the operator's QC_BROKER_MODULE. */
    broker: "mock-broker" | "broker-adapter";
    mode: "dry-run" | "live";
  };
  instrument: {
    id: number;
    symbol: string;
    asset_class: AssetClass;
  };
  poll_interval_ms: number;
  request_budget: number;
  requests_made: number;
  /** How many polls got real Level 2 depth (get_equity_price_book) vs. fell back to a
   *  top-of-book quote -- session.ts's SessionResult.depthCounts, carried into the manifest so
   *  the fallback flag survives past the process, not just in-memory (issue #43/#46 follow-up:
   *  "fall back to top-of-book with an explicit flag"). */
  depth: { l2: number; top_of_book: number };
  coverage:
    | {
        record_count: number;
        first_ts_exchange_ns: string;
        last_ts_exchange_ns: string;
        first_ts_local_iso: string;
        last_ts_local_iso: string;
      }
    | { record_count: 0 };
  quality: QualityReport;
  files: Record<string, string>;
}

function isoFromNs(ns: bigint): string {
  return new Date(Number(ns / 1_000_000n)).toISOString();
}

export interface BuildManifestOptions {
  dataset: string;
  mode: "dry-run" | "live";
  instrumentId: number;
  symbol: string;
  assetClass: AssetClass;
  pollIntervalMs: number;
  requestBudget: number;
  requestsMade: number;
  depthCounts: { l2: number; top_of_book: number };
  records: readonly SnapshotRecord[];
  quality: QualityReport;
  /** Path of the recorded CSV, relative to the repo root (used as the files-map key). */
  recordingRelPath: string;
  /** Absolute path of the recorded CSV, hashed for the files map. */
  recordingAbsPath: string;
}

export function buildManifest(opts: BuildManifestOptions): CatalogManifest {
  const coverage: CatalogManifest["coverage"] =
    opts.records.length === 0
      ? { record_count: 0 }
      : {
          record_count: opts.records.length,
          first_ts_exchange_ns: opts.records[0]!.tsExchangeNs.toString(),
          last_ts_exchange_ns: opts.records[opts.records.length - 1]!.tsExchangeNs.toString(),
          first_ts_local_iso: isoFromNs(opts.records[0]!.tsLocalNs),
          last_ts_local_iso: isoFromNs(opts.records[opts.records.length - 1]!.tsLocalNs),
        };
  return {
    dataset: opts.dataset,
    schema_version: "recorder-catalog-v1",
    catalog_written_at: new Date().toISOString(),
    record_format: "engine/crates/gateway/src/record.rs",
    source: { broker: opts.mode === "dry-run" ? "mock-broker" : "broker-adapter", mode: opts.mode },
    instrument: { id: opts.instrumentId, symbol: opts.symbol, asset_class: opts.assetClass },
    poll_interval_ms: opts.pollIntervalMs,
    request_budget: opts.requestBudget,
    requests_made: opts.requestsMade,
    depth: opts.depthCounts,
    coverage,
    quality: opts.quality,
    files: { [opts.recordingRelPath]: sha256File(opts.recordingAbsPath) },
  };
}

/** Writes the manifest to data/catalog/<dataset>.json and returns the path written. */
export function writeManifest(manifest: CatalogManifest, catalogDir: string): string {
  mkdirSync(catalogDir, { recursive: true });
  const filePath = path.join(catalogDir, `${manifest.dataset}.json`);
  writeFileSync(filePath, `${JSON.stringify(manifest, null, 1)}\n`, "utf8");
  return filePath;
}
