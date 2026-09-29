// Append-only decision ledger: out/agent/ledger.jsonl (gitignored). Every entry is validated
// against decision_ledger_entry before it's written; a bad entry throws instead of corrupting
// the file. Never truncates or rewrites — only appendFileSync.
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { ledgerPath as defaultLedgerPath } from "./paths.js";
import { validateOrThrow } from "./schema.js";
import type { DecisionLedgerEntry } from "./types.js";

export function appendLedgerEntry(entry: DecisionLedgerEntry, filePath: string = defaultLedgerPath()): void {
  validateOrThrow("decision_ledger_entry", entry);
  mkdirSync(path.dirname(filePath), { recursive: true });
  appendFileSync(filePath, `${JSON.stringify(entry)}\n`, "utf8");
}

export function ledgerSha256(filePath: string = defaultLedgerPath()): string {
  const contents = existsSync(filePath) ? readFileSync(filePath) : Buffer.alloc(0);
  return createHash("sha256").update(contents).digest("hex");
}

/**
 * Same as {@link ledgerSha256}, except every `result.approval.signature` (order or cancel) is
 * replaced with a fixed placeholder first.
 *
 * Wave-2 integration finding: `external` venue mode's approval is signed with a fresh Ed25519
 * keypair the real qc-bridge generates once per process and never persists (protocol v1.2,
 * "so an old approval cannot outlive the process that minted it" -- a deliberate security
 * property, not a bug). Every OTHER field a ledger entry records is fully determined by the
 * recording/limits/model/mock-broker inputs (client_order_id, instrument, side, qty, price,
 * request_id, time_in_force, expires_ts_ns all come from the sim/market clock or the fixed
 * inputs), but the signature bytes themselves depend on that per-process random key, so two
 * independent runs against the real binary can never produce a byte-identical raw ledger file --
 * only a fixed test key (this lane's own fake-bridge-v12.ts uses one on purpose) could do that,
 * and the real bridge deliberately does not. This function is what "run twice, prove the
 * decision/order/execution content is identical" actually means once a real, correctly
 * non-deterministic signing key is involved; `ledgerSha256` above is unchanged and still reports
 * the true, non-redacted file hash for anything that needs the literal bytes on disk.
 */
export function ledgerSha256RedactingApprovalSignatures(filePath: string = defaultLedgerPath()): string {
  const contents = existsSync(filePath) ? readFileSync(filePath, "utf8") : "";
  const redacted = contents
    .split("\n")
    .map((line) => {
      if (!line.trim()) return line;
      const entry = JSON.parse(line) as Record<string, unknown>;
      const result = entry.result as { approval?: { signature?: string } } | undefined;
      if (result?.approval?.signature) result.approval.signature = "<redacted-per-process-random-signature>";
      return JSON.stringify(entry);
    })
    .join("\n");
  return createHash("sha256").update(redacted).digest("hex");
}
