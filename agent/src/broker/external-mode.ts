// Wires BrokerGateway (and its dependencies) into "external" venue mode: the v1.1 hello
// handshake for the approval public key, the bridge's own market clock for approval expiry
// (never Date.now() -- see gateway-market-clock.test.ts), and reconciliation triggered from
// every point wave2-spec.md names: at startup (call `reconcile()` once before the loop starts),
// periodically (agent/src/loop.ts calls it every N decisions), and after an ambiguous broker
// response (wired here as the gateway's onAmbiguousOutcome). agent/src/cli.ts is the only
// production caller; tests build BrokerGateway/reconcileWithBroker directly instead of going
// through this factory, the same way agent/tests/broker/gateway.test.ts already does.
import { ed25519PublicKeyFromRaw } from "./crypto.js";
import { BrokerGateway, type GatewayConfig } from "./gateway.js";
import { Heartbeat } from "./heartbeat.js";
import { reconcileWithBroker } from "./reconcile.js";
import type { BrokerAdapter } from "./adapter.js";
import type { AssetClass } from "./types.js";
import type { BridgeClient } from "../bridge.js";

export interface ExternalModeDeps {
  bridge: BridgeClient;
  broker: BrokerAdapter;
  assetClass: AssetClass;
  instrument: string;
  journalPath?: string;
  iocCancelAfterMs?: number;
  /** Passed through to reconcileWithBroker's statusPath (preflight's reconcile_recent gate). */
  reconcileStatusPath?: string;
  requestTimeoutMs?: number;
  logPreview?: GatewayConfig["logPreview"];
  /** Issue #44 reopened: overrides the heartbeat's own status-poll cadence (default 1s). Tests
   *  use this to make the "observes a halt within budget" property fast to assert. */
  heartbeatIntervalMs?: number;
  /** Fires the first time the heartbeat observes the bridge halted (see heartbeat.ts). */
  onHalted?: (reason: string) => void;
}

export interface ExternalMode {
  gateway: BrokerGateway;
  /** Runs one reconciliation pass now. Call at startup and periodically (agent/src/loop.ts);
   *  also wired automatically below as the gateway's onAmbiguousOutcome trigger. */
  reconcile: () => Promise<void>;
  /** Issue #44 reopened: a real wall-clock timer, independent of the decision loop, that keeps
   *  polling the bridge and finishes any halt-triggered cancels at the broker even if the loop
   *  itself is stuck. Caller starts it once external mode is fully wired (this factory does not
   *  start it itself, so a caller that wants to build the gateway without yet running a live
   *  heartbeat -- e.g. a test asserting on one tick at a time -- can). Always stop() it in the
   *  same place the bridge itself gets shut down. */
  heartbeat: Heartbeat;
}

/**
 * Performs the hello handshake and builds an external-venue-mode BrokerGateway wired to: the
 * bridge itself as its BridgeReporter (report_execution) and BridgeReconciler (reconcile), the
 * bridge's market clock (`marketNowNs()`) for approval expiry, and `reconcileWithBroker()`
 * triggered on every ambiguous broker response. Throws if the bridge reports venue_mode "sim" --
 * this factory is external-mode-only; sim mode needs no gateway at all (the bridge routes
 * straight to SimVenue and never returns an approval to verify). Also throws, before the bridge
 * is asked anything, when the broker adapter's verifyAccount() refuses the account.
 */
export async function createExternalModeGateway(deps: ExternalModeDeps): Promise<ExternalMode> {
  await deps.broker.verifyAccount({ timeoutMs: deps.requestTimeoutMs });
  const helloResp = await deps.bridge.hello();
  if (!helloResp.ok || !helloResp.hello) {
    throw new Error(`bridge hello failed: ${helloResp.error?.code ?? "unknown"} ${helloResp.error?.message ?? ""}`);
  }
  if (helloResp.hello.venue_mode !== "external") {
    throw new Error(`createExternalModeGateway called but the bridge reported venue_mode ${helloResp.hello.venue_mode}`);
  }
  const approvalPublicKey = ed25519PublicKeyFromRaw(Buffer.from(helloResp.hello.approval_public_key, "base64"));

  const reconcile = (): Promise<void> =>
    reconcileWithBroker({
      broker: deps.broker,
      bridge: deps.bridge,
      assetClass: deps.assetClass,
      instrument: deps.instrument,
      clientOrderIdForRefId: (refId) => gateway.clientOrderIdForRefId(refId),
      callOpts: { timeoutMs: deps.requestTimeoutMs },
      statusPath: deps.reconcileStatusPath,
    });

  const gateway = new BrokerGateway({
    broker: deps.broker,
    reporter: deps.bridge,
    approvalPublicKey,
    iocCancelAfterMs: deps.iocCancelAfterMs,
    now: () => deps.bridge.marketNowNs(),
    requestTimeoutMs: deps.requestTimeoutMs,
    journalPath: deps.journalPath,
    logPreview: deps.logPreview,
    onAmbiguousOutcome: () => {
      // Fire and forget: GatewayConfig's contract for this hook is that it must never itself
      // throw or be awaited (it runs inside a catch block that's about to throw the order's own
      // error). Log-and-continue is the right failure mode for a reconcile-trigger failure, not
      // crashing the whole loop over it.
      reconcile().catch((err: unknown) => {
        console.error("reconciliation after an ambiguous broker response failed:", err);
      });
    },
  });

  const heartbeat = new Heartbeat({
    bridge: deps.bridge,
    gateway,
    instrument: deps.instrument,
    assetClass: deps.assetClass,
    intervalMs: deps.heartbeatIntervalMs,
    onHalted: deps.onHalted,
  });

  return { gateway, reconcile, heartbeat };
}
