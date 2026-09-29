// Issue #45 (TS side): "check every day that our books match the broker's." This module pulls
// what the broker itself reports -- every order, this instrument's position, and account cash
// -- through the BrokerAdapter (adapter.ts) and hands it to the bridge's protocol v1.2
// `reconcile` op. The bridge owns the actual comparison (it holds the OMS's ground truth); this
// module's job is to gather the venue-side snapshot honestly and to fail loudly, never silently,
// when the bridge reports a mismatch -- root CLAUDE.md rule 10: "neither side is silently
// trusted over the other."
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import { bridgeVenueOrderId, type BrokerAdapter, type CallOptions, type VenueOrderRecord } from "./adapter.js";
import type { AssetClass } from "./types.js";
import type { VenueOrderSnapshot, VenueSnapshot } from "../types.js";

/** Thrown when the bridge reports the reconcile call did not come back clean. By the time this
 *  throws, the bridge has already engaged its own halt (wave2-spec.md's reconcile op: "any
 *  discrepancy ... engages halt via the bridge"); this exception is what stops the TS side from
 *  treating a mismatch as fine and continuing to trade regardless. */
export class ReconciliationHalted extends Error {
  constructor(
    message: string,
    public readonly detail?: Record<string, unknown>,
  ) {
    super(`reconciliation mismatch: ${message}`);
    this.name = "ReconciliationHalted";
  }
}

/** The subset of BridgeClient this module needs -- narrowed so a test can hand in a trivial
 *  double instead of a real bridge process, same convention as broker/types.ts's
 *  BridgeReporter.
 *
 *  Wave-2 review fix: the real bridge's `reconcile` op (schemas/decision/v1/README.md's v1.2
 *  section) always answers the RPC envelope's `ok:true` once the request itself was
 *  well-formed -- a *successful comparison that found a mismatch* is not a protocol error. The
 *  actual clean/mismatch verdict is nested under `reconcile: {ok, discrepancies}`. An earlier
 *  version of this interface (and `reconcileWithBroker` below) checked the envelope's `ok`
 *  instead, which this lane's own fake bridge happened to set to `false` on a mismatch -- a
 *  shape the real bridge does not use -- so `ReconciliationHalted` would never have fired against
 *  the real binary. */
export interface BridgeReconciler {
  reconcile(venue: VenueSnapshot): Promise<{
    ok: boolean;
    error?: { code: string; message: string };
    reconcile?: { ok: boolean; discrepancies: string[] };
  }>;
}

export interface ReconcileDeps {
  broker: BrokerAdapter;
  bridge: BridgeReconciler;
  assetClass: AssetClass;
  instrument: string;
  /** Maps a venue order's ref_id back to our client_order_id (BrokerGateway's journal); a venue
   *  order without one is unknown to us, and the bridge flags it if it is open. */
  clientOrderIdForRefId: (refId: string) => number | null;
  callOpts?: CallOptions;
  /** Where each pass records its verdict for scripts/ops/preflight.py's reconcile_recent gate
   *  (its DEFAULT_RECONCILE_STATUS). Omitted, nothing is written, and preflight fails closed on
   *  the missing file. */
  statusPath?: string;
}

/** Writes preflight's contract, `{"ts_ns":int,"ok":bool,"discrepancies":[str]}`, atomically
 *  (temp file + rename) so preflight never reads a half-written file. `ts_ns` is wall-clock
 *  time, not the bridge's market clock, because preflight compares it against its own
 *  `time.time_ns()`; it is serialized from a bigint since nanoseconds exceed 2^53. */
export function writeReconcileStatus(statusPath: string, ok: boolean, discrepancies: string[]): void {
  const tsNs = BigInt(Date.now()) * 1_000_000n;
  const body = `{"ts_ns":${tsNs},"ok":${ok},"discrepancies":${JSON.stringify(discrepancies)}}\n`;
  mkdirSync(path.dirname(statusPath), { recursive: true });
  const tmp = `${statusPath}.${process.pid}.tmp`;
  writeFileSync(tmp, body, "utf8");
  renameSync(tmp, statusPath);
}

/** The bridge's reconcile schema knows live ("open") or terminal venue states, and a u64 venue
 *  order id (adapter.ts's bridgeVenueOrderId, the same id the gateway reports on accept). An
 *  order with no id is not something the bridge can compare, so it fails the pass. */
function toVenueOrderSnapshot(order: VenueOrderRecord, clientOrderIdForRefId: ReconcileDeps["clientOrderIdForRefId"]): VenueOrderSnapshot {
  if (!order.venue_order_id) throw new Error("the broker listed an order with no id");
  const state = order.status === "accepted" || order.status === "partially_filled" ? "open" : order.status;
  return {
    client_order_id: order.ref_id === undefined ? null : clientOrderIdForRefId(order.ref_id),
    venue_order_id: bridgeVenueOrderId(order.venue_order_id),
    state,
    filled_qty: order.filled_qty ?? "0",
  };
}

/** Runs one reconciliation pass. Call this at loop startup, periodically, and after any
 *  ambiguous broker response (wave2-spec.md) -- `agent/src/broker/external-mode.ts` wires all
 *  three trigger points to this same function, so there is exactly one place that decides what
 *  "reconciled" means. */
export async function reconcileWithBroker(deps: ReconcileDeps): Promise<void> {
  try {
    await compareWithBroker(deps);
  } catch (err) {
    // Any failure, including a broker read that never reached the bridge, replaces the last
    // recorded verdict: an older clean result must not keep preflight passing after this.
    if (deps.statusPath !== undefined) {
      const discrepancies =
        err instanceof ReconciliationHalted && Array.isArray(err.detail?.discrepancies)
          ? (err.detail.discrepancies as string[])
          : [err instanceof Error ? err.message : String(err)];
      writeReconcileStatus(deps.statusPath, false, discrepancies);
    }
    throw err;
  }
  if (deps.statusPath !== undefined) writeReconcileStatus(deps.statusPath, true, []);
}

async function compareWithBroker(deps: ReconcileDeps): Promise<void> {
  const [orders, account] = await Promise.all([
    deps.broker.listOrders(deps.assetClass, deps.callOpts),
    deps.broker.getAccountState(deps.assetClass, deps.instrument, deps.callOpts),
  ]);

  const venue: VenueSnapshot = {
    orders: orders.map((o) => toVenueOrderSnapshot(o, deps.clientOrderIdForRefId)),
    position: account.position,
    cash: account.cash,
  };

  const resp = await deps.bridge.reconcile(venue);
  if (!resp.ok) {
    // The RPC itself failed (a protocol error, e.g. a malformed request) -- distinct from a
    // clean-comparison-but-mismatched result, which lands in the branch below.
    throw new ReconciliationHalted(resp.error?.message ?? "the reconcile request itself failed", { error: resp.error });
  }
  if (!resp.reconcile) {
    // The real bridge always nests its verdict (wire.rs); a response without one is not clean.
    throw new ReconciliationHalted("the bridge answered reconcile without a verdict", { response: resp });
  }
  if (!resp.reconcile.ok) {
    throw new ReconciliationHalted(resp.reconcile.discrepancies.join("; ") || "the bridge reported a discrepancy", {
      discrepancies: resp.reconcile.discrepancies,
    });
  }
}
