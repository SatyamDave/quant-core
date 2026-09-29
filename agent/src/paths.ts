// Locates repo-root-relative paths regardless of whether this runs from src/ (tsx) or
// dist/ (tsc build), and regardless of the caller's cwd.
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

function findUp(startDir: string, isRoot: (dir: string) => boolean, maxLevels = 8): string {
  let dir = startDir;
  for (let i = 0; i < maxLevels; i++) {
    if (isRoot(dir)) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`could not locate the quant-core repo root walking up from ${startDir}`);
}

let cachedRoot: string | undefined;

export function repoRoot(): string {
  if (cachedRoot) return cachedRoot;
  const here = path.dirname(fileURLToPath(import.meta.url));
  cachedRoot = findUp(here, (dir) => existsSync(path.join(dir, "schemas", "decision", "v1", "README.md")));
  return cachedRoot;
}

export function schemaDir(): string {
  return path.join(repoRoot(), "schemas", "decision", "v1");
}

export function ledgerPath(): string {
  return process.env.QC_AGENT_LEDGER_PATH ?? path.join(repoRoot(), "out", "agent", "ledger.jsonl");
}

/** Fsync'd append-only journal of broker order-submission attempts (agent/src/broker/journal.ts).
 *  Gitignored, same as ledgerPath() -- out/agent/ already covers it. */
export function brokerJournalPath(): string {
  return process.env.QC_AGENT_BROKER_JOURNAL_PATH ?? path.join(repoRoot(), "out", "agent", "broker-journal.jsonl");
}

/** Latest reconcile verdict (agent/src/broker/reconcile.ts); must match
 *  scripts/ops/preflight.py's DEFAULT_RECONCILE_STATUS. Gitignored via ops/live/state/. */
export function reconcileStatusPath(): string {
  return process.env.QC_RECONCILE_STATUS_PATH ?? path.join(repoRoot(), "ops", "live", "state", "reconcile-status.json");
}
