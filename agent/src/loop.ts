// The decision loop: next_decision_request -> decide -> submit_order_intent or no_trade ->
// ledger, until the bridge's feed is exhausted (decision_request: null).
import type { BrokerGateway } from "./broker/gateway.js";
import type { Heartbeat } from "./broker/heartbeat.js";
import type { AssetClass } from "./broker/types.js";
import type { BridgeClient } from "./bridge.js";
import type { Decider } from "./decider/types.js";
import { appendLedgerEntry } from "./ledger.js";
import type { Decision, DecisionRequest, IntentResult, LedgerMode, Side } from "./types.js";

function isTradeDecision(decision: Decision): decision is Decision & { action: Side } {
  return decision.action !== "no_trade";
}

export interface LoopCounts {
  decisions: number;
  accepted: number;
  rejected: number;
  noTrades: number;
}

/** External (protocol v1.2, non-sim) venue mode's extra wiring: absent entirely for sim mode,
 *  which needs none of this (the bridge routes accepted intents straight to SimVenue and never
 *  returns an approval). agent/src/cli.ts builds this via
 *  agent/src/broker/external-mode.ts's createExternalModeGateway() once the bridge's hello
 *  handshake reports venue_mode "external". */
export interface ExternalModeLoopOptions {
  gateway: BrokerGateway;
  assetClass: AssetClass;
  /** Runs one reconciliation pass (issue #45). Called once before the loop's first decision
   *  (startup) and every `reconcileEveryNDecisions` decisions after that (periodic) --
   *  wave2-spec.md's third trigger point, after an ambiguous broker response, is wired directly
   *  into the gateway itself (see createExternalModeGateway's onAmbiguousOutcome). */
  reconcile: () => Promise<void>;
  /** Default chosen to reconcile often enough to matter in a demo-length run
   *  (`just agent-sim-external`) without adding a bridge round trip to every single decision. */
  reconcileEveryNDecisions?: number;
  /** Issue #44 reopened: threaded through only so a caller that already started it
   *  (agent/src/cli.ts, agent/src/broker/external-mode.js's createExternalModeGateway) has one
   *  place to find it again for stop()ing at shutdown. runLoop itself never calls into this --
   *  the entire point is that the heartbeat runs on its own timer, independent of this loop. */
  heartbeat?: Heartbeat;
}

export interface LoopOptions {
  bridge: BridgeClient;
  decider: Decider;
  mode: LedgerMode;
  promptVersion: string;
  model: string;
  ledgerPath?: string;
  external?: ExternalModeLoopOptions;
}

const DEFAULT_RECONCILE_EVERY_N_DECISIONS = 20;

export async function runLoop(opts: LoopOptions): Promise<LoopCounts> {
  const counts: LoopCounts = { decisions: 0, accepted: 0, rejected: 0, noTrades: 0 };
  const reconcileEveryN = opts.external?.reconcileEveryNDecisions ?? DEFAULT_RECONCILE_EVERY_N_DECISIONS;

  // Startup reconciliation (issue #45, wave2-spec.md): external mode refuses new intents until
  // the first clean reconcile anyway (the bridge's own rule), but doing it here, before the loop
  // asks for a single decision, means we find that out immediately rather than after the first
  // order attempt fails for a reason that looks unrelated.
  if (opts.external) await opts.external.reconcile();

  for (;;) {
    const next = await opts.bridge.nextDecisionRequest();
    if (!next.ok) {
      throw new Error(`bridge next_decision_request failed: ${next.error?.code} ${next.error?.message}`);
    }
    const request: DecisionRequest | null | undefined = next.decision_request;
    if (!request) break; // feed exhausted

    const startedAt = Date.now();
    const { decision, raw, costUsd } = await opts.decider.decide(request);
    // Wall-clock latency is recorded only for live decisions. Fake and replay deciders do no
    // real work, so their timing is noise, and recording it makes the ledger differ between two
    // runs of the same input (replay's whole contract is that it doesn't).
    const latencyMs = opts.mode === "live" ? Date.now() - startedAt : undefined;
    counts.decisions += 1;

    const intentResult = isTradeDecision(decision)
      ? await submitOrder(opts.bridge, request, decision, counts, opts.external)
      : await submitNoTrade(opts.bridge, decision.request_id, decision.rationale, counts);

    appendLedgerEntry(
      {
        request,
        decision,
        result: intentResult,
        mode: opts.mode,
        prompt_version: opts.promptVersion,
        model: opts.model,
        raw: raw ?? null,
        cost_usd: costUsd,
        latency_ms: latencyMs,
      },
      opts.ledgerPath,
    );

    // Periodic reconciliation: decision-count-based, not wall-clock-based, so a run is
    // deterministic and testable (no timer to race) -- appropriate for a loop whose own cadence
    // is "one decision at a time" rather than a fixed wall-clock interval.
    if (opts.external && counts.decisions % reconcileEveryN === 0) {
      await opts.external.reconcile();
    }
  }

  return counts;
}

async function submitNoTrade(
  bridge: BridgeClient,
  requestId: string,
  reason: string,
  counts: LoopCounts,
): Promise<null> {
  const resp = await bridge.noTrade(requestId, reason);
  if (!resp.ok) {
    throw new Error(`bridge no_trade failed: ${resp.error?.code} ${resp.error?.message}`);
  }
  counts.noTrades += 1;
  return null;
}

async function submitOrder(
  bridge: BridgeClient,
  request: DecisionRequest,
  decision: Decision & { action: Side },
  counts: LoopCounts,
  external: ExternalModeLoopOptions | undefined,
): Promise<IntentResult | null> {
  if (!decision.qty || !decision.limit_price) {
    throw new Error(`decision for ${decision.request_id} is ${decision.action} but is missing qty/limit_price`);
  }
  const intent = {
    request_id: decision.request_id,
    instrument: request.instrument,
    side: decision.action,
    qty: decision.qty,
    limit_price: decision.limit_price,
    time_in_force: "ioc" as const,
    reason: decision.rationale,
  };
  const resp = await bridge.submitOrderIntent(intent);
  if (!resp.ok) {
    throw new Error(`bridge submit_order_intent failed: ${resp.error?.code} ${resp.error?.message}`);
  }
  const result = resp.result ?? null;
  if (result?.accepted) counts.accepted += 1;
  else counts.rejected += 1;

  // External mode, accepted: the bridge has recorded PendingNew and handed back an approval, but
  // has NOT routed the order anywhere (schemas/decision/v1/README.md's v1.1 section) -- nothing
  // reaches the broker until the gateway itself verifies and executes it. This is exactly the
  // "no end-to-end external-mode run" gap: without this call, submit_order_intent's acceptance
  // was the whole story and the order never actually went anywhere.
  if (external && result?.accepted && result.approval && result.client_order_id !== undefined) {
    await external.gateway.submitOrder(
      {
        client_order_id: result.client_order_id,
        instrument: intent.instrument,
        side: intent.side,
        qty: intent.qty,
        limit_price: intent.limit_price,
        time_in_force: intent.time_in_force,
        request_id: intent.request_id,
      },
      result.approval,
      external.assetClass,
    );
  }

  return result;
}
