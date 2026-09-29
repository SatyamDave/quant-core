// Fsync'd, append-only journal of every broker order-submission attempt (out/agent/broker-journal.jsonl
// by default, gitignored -- see agent/src/paths.ts's brokerJournalPath()). Required because
// BrokerGateway's in-memory idempotency map alone does not survive a process restart: if the process
// crashes between the broker receiving an order and this process learning the outcome, an in-memory-only
// map would forget the order was ever sent, and the next process would blind-resubmit it -- a real
// double order. recordAttempt() is written and fsync'd BEFORE the network call, with the ref_id that
// call carries, so after a crash the next process can look the order up by that ref_id (and, if it
// must resend, reuse it so the broker deduplicates). replayJournal() seeds BrokerGateway from this file.
//
// Every entry carries `session`, the approval public key of the qc-bridge process that approved
// the order. The bridge restarts client_order_id at 0 and mints a fresh key on every start, so an
// id is only unique within one key: replay matches only the current session's entries.
import { createHash } from "node:crypto";
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, writeSync } from "node:fs";
import path from "node:path";
import type { PlaceOrderResult } from "./types.js";

/** What BrokerGateway needs restored after a restart. "in_flight" is same-process only (an
 *  unresolved Promise cannot be persisted), so it replays as "unknown". `sends` counts the place
 *  calls already made for this order, so the resend bound survives a restart too. */
export type ReplayedOrderState =
  | { state: "unknown"; refId: string; sentAt: string; sends: number }
  | { state: "terminal"; refId: string; result: PlaceOrderResult };

interface AttemptEntry {
  phase: "attempt";
  session?: string;
  client_order_id: number;
  ref_id: string;
  /** ISO 8601 time of the first send (bridge market clock), the lower bound for a lookup. */
  sent_at: string;
}

interface ResolvedEntry {
  phase: "resolved";
  session?: string;
  client_order_id: number;
  ref_id: string;
  result: JournaledResult;
}

type JournalEntry = AttemptEntry | ResolvedEntry;

/** Only the normalized fields are journaled: a venue response can echo the account number. */
type JournaledResult = Omit<PlaceOrderResult, "raw"> & { ref_id?: string };

function journaledResult(result: PlaceOrderResult & { ref_id?: string }): JournaledResult {
  const { status, venue_order_id, ref_id, filled_qty, avg_price, reason } = result;
  return { status, venue_order_id, ref_id, filled_qty, avg_price, reason };
}

// An entry without a ref_id (written before ref_id existed) fails closed: without it the order
// cannot be looked up, and resending under a new ref_id could double it.
function isJournalEntry(value: unknown): value is JournalEntry {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  if (typeof v.client_order_id !== "number" || typeof v.ref_id !== "string") return false;
  // An entry without a session predates session scoping; it is kept as another session's entry.
  if (v.session !== undefined && typeof v.session !== "string") return false;
  if (v.phase === "attempt") return typeof v.sent_at === "string";
  if (v.phase === "resolved") return typeof v.result === "object" && v.result !== null;
  return false;
}

export class Journal {
  constructor(
    private readonly filePath: string,
    private readonly session: string,
  ) {
    mkdirSync(path.dirname(filePath), { recursive: true });
  }

  private appendFsync(entry: JournalEntry): void {
    const fd = openSync(this.filePath, "a");
    try {
      writeSync(fd, `${JSON.stringify(entry)}\n`, null, "utf8");
      fsyncSync(fd); // durability: a crash right after this line still has the entry on disk
    } finally {
      closeSync(fd);
    }
  }

  /** Must be called BEFORE every place call, including a resend. */
  recordAttempt(clientOrderId: number, refId: string, sentAt: string): void {
    this.appendFsync({ phase: "attempt", session: this.session, client_order_id: clientOrderId, ref_id: refId, sent_at: sentAt });
  }

  /** Called once an outcome is known; `result.venue_order_id` is what cancels and lookups use. */
  recordResolved(clientOrderId: number, refId: string, result: PlaceOrderResult): void {
    this.appendFsync({ phase: "resolved", session: this.session, client_order_id: clientOrderId, ref_id: refId, result: journaledResult(result) });
  }
}

/** `session` is the bridge's hello approval public key (base64 of the raw 32 bytes). */
export function openJournal(filePath: string, session: string): Journal {
  return new Journal(filePath, session);
}

export interface JournalReplay {
  /** The current session's orders, by client_order_id. */
  orders: Map<number, ReplayedOrderState>;
  /** ref_ids of other sessions' orders still unknown or live when their session ended. Never
   *  matched to a current client_order_id; left for reconciliation to find at the venue. */
  otherSessionsUnresolved: string[];
}

const LIVE: ReadonlySet<PlaceOrderResult["status"]> = new Set(["accepted", "partially_filled"]);

/** Replays every line; the last entry per (session, client_order_id) wins. A current-session id
 *  whose last entry is an attempt was sent with no confirmed outcome and comes back "unknown",
 *  which forces a lookup by ref_id before any resend. A corrupt or unrecognized line throws
 *  rather than being dropped. */
export function replayJournal(filePath: string, session: string): JournalReplay {
  const out = new Map<number, ReplayedOrderState>();
  // Keyed by (session, client_order_id); the value is the last entry's ref_id and whether it was done.
  const others = new Map<string, { refId: string; done: boolean }>();
  if (!existsSync(filePath)) return { orders: out, otherSessionsUnresolved: [] };
  const sends = new Map<number, number>();
  const firstSentAt = new Map<number, string>();
  const lines = readFileSync(filePath, "utf8").split("\n").filter((l) => l.length > 0);
  for (const line of lines) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      throw new Error(`broker journal ${filePath} contains a corrupt line: ${line.slice(0, 200)}`);
    }
    if (!isJournalEntry(parsed)) {
      throw new Error(`broker journal ${filePath} contains an entry with an unrecognized shape: ${line.slice(0, 200)}`);
    }
    if (parsed.session !== session) {
      const done = parsed.phase === "resolved" && !LIVE.has(parsed.result.status);
      others.set(JSON.stringify([parsed.session ?? null, parsed.client_order_id]), { refId: parsed.ref_id, done });
      continue;
    }
    const id = parsed.client_order_id;
    if (parsed.phase === "attempt") {
      const count = (sends.get(id) ?? 0) + 1;
      sends.set(id, count);
      if (!firstSentAt.has(id)) firstSentAt.set(id, parsed.sent_at);
      out.set(id, { state: "unknown", refId: parsed.ref_id, sentAt: firstSentAt.get(id)!, sends: count });
    } else {
      out.set(id, { state: "terminal", refId: parsed.ref_id, result: { ...parsed.result, raw: {} } });
    }
  }
  const otherSessionsUnresolved = [...others.values()].filter((o) => !o.done).map((o) => o.refId);
  return { orders: out, otherSessionsUnresolved };
}

/** sha256 of the journal with each distinct `session` replaced by its order of first appearance.
 *  The session is the bridge's per-process random key, so two runs that sent identical orders
 *  differ only there; this is the hash that proves they match. */
export function journalSha256RedactingSessions(filePath: string): string {
  const contents = existsSync(filePath) ? readFileSync(filePath, "utf8") : "";
  const seen = new Map<string, string>();
  const redacted = contents
    .split("\n")
    .map((line) => {
      if (!line.trim()) return line;
      const entry = JSON.parse(line) as Record<string, unknown>;
      if (typeof entry.session === "string") {
        if (!seen.has(entry.session)) seen.set(entry.session, `<session-${seen.size + 1}>`);
        entry.session = seen.get(entry.session);
      }
      return JSON.stringify(entry);
    })
    .join("\n");
  return createHash("sha256").update(redacted).digest("hex");
}
