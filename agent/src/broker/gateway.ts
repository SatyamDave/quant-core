// The door orders must pass through to reach the broker (issue #35). This is a separate code
// path from the agent's reasoning loop: it is never registered as an SDK tool (agent/CLAUDE.md,
// ADR-0040 §3), and agent/tests/broker/no-decider-import.test.ts proves nothing under
// agent/src/decider/ can import this directory. It only ever executes an order qc-bridge has
// already approved: verify the v1.1 approval stamp against the hello public key, check the
// payload matches the order field-by-field, reject anything expired, never resubmit a
// client_order_id we've already resolved, and never resend an order whose outcome is unknown
// without first looking it up by its ref_id. A resend reuses that ref_id (derived from the signed
// approval payload), so the venue deduplicates it even when the lookup missed a real order.
//
// IOC is emulated here, so it works with any broker: an approved "ioc" order is sent as a resting
// limit order (the adapter picks the venue's day time in force), then cancelled by this gateway
// once iocCancelAfterMs has passed (or at once, if a halt asks for the
// cancel first). Its final state is read back from the venue, never assumed from the cancel call,
// and reported to the bridge.
import type { KeyObject } from "node:crypto";
import { fromFixed, toFixed } from "../decimal.js";
import { brokerJournalPath as defaultBrokerJournalPath } from "../paths.js";
import { verifyEd25519 } from "./crypto.js";
import { openJournal, replayJournal, type Journal } from "./journal.js";
import { bridgeVenueOrderId, orderRefusal, refIdForApproval, type BrokerAdapter, type VenueOrderRecord } from "./adapter.js";
import type {
  Approval,
  ApprovalPayload,
  ApprovedOrder,
  AssetClass,
  BridgeReporter,
  CancelApprovalPayload,
  CancelOrderResult,
  DecimalString,
  ExecutionEvent,
  PlaceOrderResult,
  PreviewResult,
} from "./types.js";

export class GatewayRejection extends Error {
  constructor(reason: string) {
    super(`approval rejected: ${reason}`);
    this.name = "GatewayRejection";
  }
}

interface Fill {
  qty: DecimalString;
  price: DecimalString;
}

type OrderState =
  | { state: "in_flight"; refId: string; promise: Promise<PlaceOrderResult> }
  | { state: "unknown"; refId: string; sentAt: string; sends: number }
  // `reported` is the fill already reported to the bridge for a still-live order, so a later
  // report sends only the difference. In memory only: a restarted bridge has no OMS to add to.
  | { state: "terminal"; refId: string; result: PlaceOrderResult; reported?: Fill };

const TERMINAL: ReadonlySet<PlaceOrderResult["status"]> = new Set(["filled", "canceled", "rejected"]);

/** Place calls allowed per client_order_id: the first send plus two resends, each resend only
 *  after a lookup by ref_id found nothing. */
export const MAX_SENDS = 3;
/** Cancel-then-read-back rounds before a cancel counts as unconfirmed. */
export const CANCEL_CONFIRM_ATTEMPTS = 5;
const CANCEL_CONFIRM_INTERVAL_MS = 250;
/** How long an emulated IOC order rests before this gateway cancels it. Conservative (short) on
 *  purpose; tune per venue with QC_IOC_CANCEL_AFTER_MS. */
export const DEFAULT_IOC_CANCEL_AFTER_MS = 2_000;
const DAY_MS = 86_400_000;

/** QC_IOC_CANCEL_AFTER_MS: how long an emulated IOC order rests before the gateway cancels it.
 *  Unset keeps DEFAULT_IOC_CANCEL_AFTER_MS (2000). Anything but an integer from 1 to 60000 refuses to start. */
export function iocCancelAfterMsFromEnv(raw: string | undefined): number | undefined {
  if (raw === undefined) return undefined;
  const ms = /^\d+$/.test(raw) ? Number(raw) : NaN;
  if (!(ms >= 1 && ms <= 60_000)) throw new Error(`QC_IOC_CANCEL_AFTER_MS must be an integer from 1 to 60000 ms, got ${JSON.stringify(raw)}`);
  return ms;
}

export interface GatewayConfig {
  /** The broker adapter every venue call goes through (adapter.ts). It owns the account. */
  broker: BrokerAdapter;
  reporter: BridgeReporter;
  /** Raw 32-byte Ed25519 public key from the bridge's `hello` response. */
  approvalPublicKey: KeyObject;
  /** Wall-clock nanoseconds "now", for expiry checks and the journal's send time; injectable for
   *  tests; defaults to Date.now() converted to ns. Compared directly against `expires_ts_ns` from
   *  the bridge's own clock, so the expiry check is only as trustworthy as this host's clock. */
  now?: () => number;
  /** Best-effort preview logger (issue #43: every order gets previewed first, and the preview
   *  is logged, before the real order goes out). Defaults to a no-op. */
  logPreview?: (order: ApprovedOrder, assetClass: AssetClass, preview: PreviewResult) => void;
  /** Per-call timeout for every broker call this gateway makes. Default 5s. */
  requestTimeoutMs?: number;
  /** Rest time of an emulated IOC order before this gateway cancels it
   *  (DEFAULT_IOC_CANCEL_AFTER_MS). */
  iocCancelAfterMs?: number;
  /** Fsync'd append-only journal path (default out/agent/broker-journal.jsonl, gitignored),
   *  replayed at construction so an order sent before a crash comes back as "unknown" and is
   *  looked up by ref_id, never blind-resubmitted. */
  journalPath?: string;
  /** Fires whenever an order's outcome becomes uncertain (a place call threw, or an emulated IOC
   *  cancel could not be confirmed), to trigger the wider broker/bridge reconciliation
   *  (agent/src/broker/reconcile.ts). Called from inside a catch or just before a throw, so it
   *  must never itself throw and is never awaited. */
  onAmbiguousOutcome?: (clientOrderId: number) => void;
}

const DEFAULT_REQUEST_TIMEOUT_MS = 5_000;
/** The preview is best-effort and runs while the 5 s approval ticks, so it gets less time than a
 *  place call; the approval is still re-checked after it. */
const PREVIEW_TIMEOUT_MS = 1_000;

function isApprovalPayload(value: unknown): value is ApprovalPayload {
  if (typeof value !== "object" || value === null) return false;
  const p = value as Record<string, unknown>;
  return (
    typeof p.client_order_id === "number" &&
    typeof p.expires_ts_ns === "number" &&
    typeof p.instrument === "string" &&
    typeof p.limit_price === "string" &&
    typeof p.qty === "string" &&
    typeof p.request_id === "string" &&
    (p.side === "buy" || p.side === "sell") &&
    (p.time_in_force === "ioc" || p.time_in_force === "gtc")
  );
}

/** Protocol v1.2's cancel approval payload -- a distinct, smaller shape from the order-approval
 *  payload above (agent/src/types.ts's `CancelApprovalPayload`). */
function isCancelApprovalPayload(value: unknown): value is CancelApprovalPayload {
  if (typeof value !== "object" || value === null) return false;
  const p = value as Record<string, unknown>;
  return p.action === "cancel" && typeof p.client_order_id === "number" && typeof p.expires_ts_ns === "number";
}

/** The fill not yet reported, given what was reported before and the venue's cumulative totals.
 *  The OMS adds each reported fill, so a later report carries only the new quantity, at the price
 *  that makes the notionals add up. undefined: nothing new. null: the venue's totals cannot be
 *  split exactly (or shrank), so no fill is reported and reconciliation must catch it. */
export function unreportedFill(prev: Fill | undefined, venue: Pick<PlaceOrderResult, "filled_qty" | "avg_price">): Fill | undefined | null {
  if (venue.filled_qty === undefined || venue.avg_price === undefined) return undefined;
  const qty = toFixed(venue.filled_qty);
  const price = toFixed(venue.avg_price);
  const prevQty = prev ? toFixed(prev.qty) : 0n;
  const prevNotional = prev ? prevQty * toFixed(prev.price) : 0n;
  const newQty = qty - prevQty;
  if (newQty === 0n) return undefined;
  if (newQty < 0n) return null;
  const notional = qty * price - prevNotional;
  if (notional <= 0n || notional % newQty !== 0n) return null;
  return { qty: fromFixed(newQty), price: fromFixed(notional / newQty) };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class BrokerGateway {
  private readonly orders = new Map<number, OrderState>();
  /** Emulated IOC orders resting at the venue: calling the entry ends the rest early. */
  private readonly iocWakers = new Map<number, () => void>();
  private readonly now: () => number;
  private readonly callOpts: { timeoutMs: number };
  private readonly previewOpts: { timeoutMs: number };
  private readonly iocCancelAfterMs: number;
  private readonly journal: Journal;

  constructor(private readonly config: GatewayConfig) {
    this.now = config.now ?? (() => Date.now() * 1_000_000);
    this.callOpts = { timeoutMs: config.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS };
    this.previewOpts = { timeoutMs: Math.min(this.callOpts.timeoutMs, PREVIEW_TIMEOUT_MS) };
    this.iocCancelAfterMs = config.iocCancelAfterMs ?? DEFAULT_IOC_CANCEL_AFTER_MS;
    const journalPath = config.journalPath ?? defaultBrokerJournalPath();
    // The bridge restarts client_order_id at 0 with a fresh approval key, so the key is what
    // tells this bridge process's order 1 from the last one's.
    const session = (config.approvalPublicKey.export({ type: "spki", format: "der" }) as Buffer).subarray(-32).toString("base64");
    this.journal = openJournal(journalPath, session);
    // Restart safety: an order sent before a crash is never treated as "never sent."
    const replay = replayJournal(journalPath, session);
    for (const [clientOrderId, snapshot] of replay.orders) this.orders.set(clientOrderId, snapshot);
    if (replay.otherSessionsUnresolved.length > 0) {
      // Not matched to any current order; reconciliation reports them if still open at the venue.
      console.error(
        `broker journal: ${replay.otherSessionsUnresolved.length} order(s) from earlier bridge sessions were unknown or live ` +
          `when their session ended (ref_ids ${replay.otherSessionsUnresolved.join(", ")})`,
      );
    }
  }

  /** client_order_id for a venue order's ref_id, for reconciliation (reconcile.ts). */
  clientOrderIdForRefId(refId: string): number | null {
    for (const [clientOrderId, state] of this.orders) if (state.refId === refId) return clientOrderId;
    return null;
  }

  /**
   * Verifies the approval and, only if it verifies, executes the order at the broker.
   * Throws GatewayRejection, before any network call, for a missing, malformed, tampered,
   * mismatched or expired approval.
   */
  async submitOrder(order: ApprovedOrder, approval: Approval | undefined, assetClass: AssetClass): Promise<PlaceOrderResult> {
    const { expires_ts_ns: expiresTsNs } = this.assertApproval(order, approval);
    const refusal = orderRefusal(order);
    if (refusal) throw new GatewayRejection(refusal);
    const id = order.client_order_id;

    const existing = this.orders.get(id);
    if (existing?.state === "terminal") return existing.result; // duplicate: never resent
    if (existing?.state === "in_flight") return existing.promise; // join the one in-flight attempt
    if (existing?.state === "unknown") return this.track(id, existing, this.lookUpThenResend(order, assetClass, existing, expiresTsNs));

    const refId = refIdForApproval(approval!.payload);
    const first = { state: "unknown" as const, refId, sentAt: this.nowIso(), sends: 1 };
    return this.track(id, first, this.send(order, assetClass, first, expiresTsNs));
  }

  /**
   * Protocol v1.2: cancels are approved separately from submits and verified the same way --
   * signature, field match, expiry -- before the broker is ever called. The venue's own order
   * state afterwards decides what is reported, never the cancel call's response.
   */
  async cancelOrder(clientOrderId: number, instrument: string, assetClass: AssetClass, approval: Approval | undefined): Promise<CancelOrderResult> {
    this.assertCancelApproval(clientOrderId, approval);
    const existing = this.orders.get(clientOrderId);

    // An emulated IOC order still resting: end the rest now; its own settle path cancels it,
    // reads the final state back and reports it.
    const wake = this.iocWakers.get(clientOrderId);
    if (existing?.state === "in_flight" && wake) {
      wake();
      const final = await existing.promise;
      return { status: final.status === "canceled" ? "canceled" : "cancel_rejected", raw: final.raw };
    }

    const venueOrderId = existing?.state === "terminal" ? existing.result.venue_order_id : undefined;
    if (existing?.state !== "terminal" || TERMINAL.has(existing.result.status) || !venueOrderId) {
      throw new GatewayRejection(`client_order_id ${clientOrderId} (${instrument}) has no live order to cancel`);
    }
    const { final, last } = await this.cancelAndConfirm(assetClass, venueOrderId);
    if (!final) {
      await this.reportLiveFill(clientOrderId, existing.refId, existing.result, last, existing.reported);
      const reason = `cancel not confirmed after ${CANCEL_CONFIRM_ATTEMPTS} attempts`;
      await this.report(clientOrderId, "cancel_rejected", { reason });
      return { status: "cancel_rejected", reason, raw: {} };
    }
    this.resolve(clientOrderId, existing.refId, final);
    await this.reportFinal(clientOrderId, final, existing.reported);
    return { status: final.status === "canceled" ? "canceled" : "cancel_rejected", reason: final.reason, raw: final.raw };
  }

  private nowIso(): string {
    return new Date(Math.floor(this.now() / 1_000_000)).toISOString();
  }

  /** Marks an order in flight; if the work fails without leaving a state of its own, the order
   *  goes back to `fallback` so the next call looks it up again rather than trusting a rejected
   *  promise or starting fresh. */
  private track(id: number, fallback: Extract<OrderState, { state: "unknown" }>, work: Promise<PlaceOrderResult>): Promise<PlaceOrderResult> {
    const promise = work.catch((err: unknown) => {
      if (this.orders.get(id)?.state === "in_flight") this.orders.set(id, fallback);
      throw err;
    });
    this.orders.set(id, { state: "in_flight", refId: fallback.refId, promise });
    return promise;
  }

  /** Parses `approval.payload` as JSON and verifies the signature over its exact bytes. */
  private verifyApprovalPayload(approval: Approval | undefined): unknown {
    if (!approval) throw new GatewayRejection("missing approval");

    let parsed: unknown;
    try {
      parsed = JSON.parse(approval.payload);
    } catch {
      throw new GatewayRejection("approval payload is not valid JSON");
    }

    // Authenticate the bytes before trusting anything the payload says.
    const sigOk = verifyEd25519(
      this.config.approvalPublicKey,
      Buffer.from(approval.payload, "utf8"),
      Buffer.from(approval.signature, "base64"),
    );
    if (!sigOk) throw new GatewayRejection("approval signature does not verify");

    return parsed;
  }

  private assertApproval(order: ApprovedOrder, approval: Approval | undefined): ApprovalPayload {
    const parsed = this.verifyApprovalPayload(approval);
    if (!isApprovalPayload(parsed)) {
      throw new GatewayRejection("approval payload is missing or has malformed fields");
    }

    // qty/limit_price compare by decimal VALUE: the real qc-bridge signs a fixed 8-fractional-digit
    // spelling ("1.00000000") while agent/src/decimal.ts's fromFixed() trims zeros ("1"). The
    // signature still covers the exact bytes qc-bridge signed.
    if (
      parsed.client_order_id !== order.client_order_id ||
      parsed.instrument !== order.instrument ||
      parsed.side !== order.side ||
      toFixed(parsed.qty) !== toFixed(order.qty) ||
      toFixed(parsed.limit_price) !== toFixed(order.limit_price) ||
      parsed.time_in_force !== order.time_in_force ||
      parsed.request_id !== order.request_id
    ) {
      throw new GatewayRejection("approval payload does not match the order being submitted");
    }

    if (parsed.expires_ts_ns <= this.now()) {
      throw new GatewayRejection("approval has expired");
    }
    return parsed;
  }

  private assertCancelApproval(clientOrderId: number, approval: Approval | undefined): void {
    const parsed = this.verifyApprovalPayload(approval);
    if (!isCancelApprovalPayload(parsed)) {
      throw new GatewayRejection("cancel approval payload is missing or has malformed fields");
    }
    if (parsed.client_order_id !== clientOrderId) {
      throw new GatewayRejection("cancel approval payload does not match the order being cancelled");
    }
    if (parsed.expires_ts_ns <= this.now()) {
      throw new GatewayRejection("cancel approval has expired");
    }
  }

  private ambiguous(clientOrderId: number): void {
    try {
      this.config.onAmbiguousOutcome?.(clientOrderId);
    } catch {
      // best effort: a reconcile-trigger failure must never mask the order's own error
    }
  }

  /** An unknown outcome is looked up by ref_id first. Only when no order carries it, and the
   *  approval is still unexpired, is the order sent again: with the same ref_id so the upstream
   *  deduplicates, and at most MAX_SENDS times in all. Anything left unresolved triggers
   *  reconciliation and throws; the order stays "unknown" so a later call looks it up again. */
  private async lookUpThenResend(
    order: ApprovedOrder,
    assetClass: AssetClass,
    unknown: Extract<OrderState, { state: "unknown" }>,
    expiresTsNs: number,
  ): Promise<PlaceOrderResult> {
    const id = order.client_order_id;
    // A day before the first send, so clock skew between us and the venue cannot hide the order.
    const since = new Date(Date.parse(unknown.sentAt) - DAY_MS).toISOString().slice(0, 10);
    let found: VenueOrderRecord | undefined;
    try {
      found = await this.config.broker.findOrderByRefId(assetClass, unknown.refId, order.instrument, since, this.callOpts);
    } catch (err) {
      this.ambiguous(id);
      throw new Error(`order ${id}: outcome unknown, and the lookup by ref_id failed: ${(err as Error).message}`);
    }
    if (found) return this.settle(order, assetClass, unknown.refId, found);
    if (unknown.sends >= MAX_SENDS) {
      this.ambiguous(id);
      throw new Error(`order ${id}: outcome unknown; no venue order carries its ref_id after ${unknown.sends} send(s); not sending again`);
    }
    if (expiresTsNs <= this.now()) {
      this.ambiguous(id);
      throw new Error(`order ${id}: outcome unknown and its approval has expired; not sending again`);
    }
    return this.send(order, assetClass, { ...unknown, sends: unknown.sends + 1 }, expiresTsNs);
  }

  private async send(
    order: ApprovedOrder,
    assetClass: AssetClass,
    attempt: Extract<OrderState, { state: "unknown" }>,
    expiresTsNs: number,
  ): Promise<PlaceOrderResult> {
    const id = order.client_order_id;
    // Best-effort preview: log it, but a preview failure never blocks the real order.
    try {
      const preview = await this.config.broker.previewOrder(assetClass, order, this.previewOpts);
      this.config.logPreview?.(order, assetClass, preview);
    } catch (err) {
      this.config.logPreview?.(order, assetClass, { warnings: [`preview failed: ${(err as Error).message}`], raw: {} });
    }

    // The preview took time off the approval: check it again, on the same clock, right before
    // the place call. This attempt was never sent, so the order keeps its previous send count.
    if (expiresTsNs <= this.now()) {
      if (attempt.sends === 1) this.orders.delete(id);
      else this.orders.set(id, { ...attempt, sends: attempt.sends - 1 });
      throw new GatewayRejection("approval expired before the order could be sent");
    }

    // Durable BEFORE the network call: if the process crashes after the venue receives this
    // order, the journal -- not the in-memory map -- remembers it was sent, and with which ref_id.
    this.journal.recordAttempt(id, attempt.refId, attempt.sentAt);

    let placed: PlaceOrderResult;
    try {
      placed = await this.config.broker.placeOrder(assetClass, order, attempt.refId, this.callOpts);
    } catch (err) {
      // Outcome unknown: it could have reached the venue and been lost on the way back, or never
      // arrived at all. Look it up before anything is resent.
      this.orders.set(id, attempt);
      console.error(`order ${id}: place attempt ${attempt.sends} outcome unknown: ${(err as Error).message}`);
      return this.lookUpThenResend(order, assetClass, attempt, expiresTsNs);
    }
    return this.settle(order, assetClass, attempt.refId, placed);
  }

  /** Takes a placed (or found) order to the state the bridge should see and reports it. */
  private async settle(order: ApprovedOrder, assetClass: AssetClass, refId: string, placed: PlaceOrderResult): Promise<PlaceOrderResult> {
    const id = order.client_order_id;
    if (TERMINAL.has(placed.status)) {
      this.resolve(id, refId, placed);
      await this.reportFinal(id, placed, undefined);
      return placed;
    }
    if (order.time_in_force !== "ioc") {
      // A resting gtc order: report it as the venue has it and leave it live.
      const reported = placed.status === "partially_filled" ? unreportedFill(undefined, placed) ?? undefined : undefined;
      this.resolve(id, refId, placed, reported);
      await this.report(id, placed.status === "partially_filled" ? "partially_filled" : "accepted", { ...placed, ...reported });
      return placed;
    }

    // Emulated IOC: the order is resting at the venue as a day limit order.
    const venueOrderId = placed.venue_order_id;
    if (!venueOrderId) {
      this.orders.set(id, { state: "unknown", refId, sentAt: this.nowIso(), sends: MAX_SENDS });
      this.ambiguous(id);
      throw new Error(`order ${id}: the venue accepted it but returned no order id to cancel it by`);
    }
    // Journal the venue order id before resting, so a crash mid-rest restarts with an order a
    // halt's cancel (and reconciliation) can still reach, instead of one known only by ref_id.
    this.journal.recordResolved(id, refId, placed);
    await this.report(id, "accepted", { venue_order_id: venueOrderId });
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, this.iocCancelAfterMs);
      this.iocWakers.set(id, () => {
        clearTimeout(timer);
        resolve();
      });
    });
    this.iocWakers.delete(id);

    const { final, last } = await this.cancelAndConfirm(assetClass, venueOrderId);
    if (!final) {
      // Left live and recorded as such, so a halt's cancel can still reach it. Stop here rather
      // than keep trading with an order the IOC contract says should be gone. A fill already
      // read back is reported first: it happened whether or not the cancel took.
      await this.reportLiveFill(id, refId, placed, last, undefined);
      this.ambiguous(id);
      throw new Error(
        `order ${id}: emulated IOC cancel not confirmed after ${CANCEL_CONFIRM_ATTEMPTS} attempts; ` +
          "it may still be live at the venue (a day order, so it expires at the close at the latest)",
      );
    }
    this.resolve(id, refId, final);
    await this.reportFinal(id, final, undefined);
    return final;
  }

  /** Cancels, then reads the order back until the venue reports a terminal state. The cancel
   *  call's own outcome is never trusted: a cancel that fails because the order filled first
   *  still ends with the fill being read back and reported. No `final`: still not terminal, and
   *  `last` is the most recent live state read back, if any. */
  private async cancelAndConfirm(
    assetClass: AssetClass,
    venueOrderId: string,
  ): Promise<{ final?: PlaceOrderResult; last?: PlaceOrderResult }> {
    const { broker } = this.config;
    let last: PlaceOrderResult | undefined;
    for (let attempt = 1; attempt <= CANCEL_CONFIRM_ATTEMPTS; attempt++) {
      if (attempt > 1) await sleep(CANCEL_CONFIRM_INTERVAL_MS);
      try {
        await broker.cancelOrder(assetClass, venueOrderId, this.callOpts);
      } catch (err) {
        console.error(`cancel attempt ${attempt} for venue order ${venueOrderId} failed: ${(err as Error).message}`);
      }
      try {
        const status = await broker.getOrderById(assetClass, venueOrderId, this.callOpts);
        if (status.status !== "not_found") {
          const read = { ...status, status: status.status, raw: status.raw ?? {} };
          if (TERMINAL.has(read.status)) return { final: read };
          last = read;
        }
      } catch (err) {
        console.error(`status read ${attempt} for venue order ${venueOrderId} failed: ${(err as Error).message}`);
      }
    }
    return { last };
  }

  /** An order whose cancel was not confirmed stays live. Any fill `last` read back beyond what was
   *  already reported is reported now, and the order is kept with what has been reported. */
  private async reportLiveFill(id: number, refId: string, known: PlaceOrderResult, last: PlaceOrderResult | undefined, reported: Fill | undefined): Promise<void> {
    const fill = last ? unreportedFill(reported, last) : undefined;
    if (fill === null) {
      console.error(`order ${id}: venue fill totals cannot be reported exactly; leaving it to reconciliation`);
      this.ambiguous(id);
    }
    const state = last ? { ...last, venue_order_id: last.venue_order_id ?? known.venue_order_id } : known;
    this.resolve(id, refId, state, fill ? { qty: state.filled_qty!, price: state.avg_price! } : reported);
    if (fill) await this.report(id, "partially_filled", { ...state, ...fill });
  }

  private resolve(id: number, refId: string, result: PlaceOrderResult, reported?: Fill): void {
    this.orders.set(id, { state: "terminal", refId, result, reported });
    this.journal.recordResolved(id, refId, result);
  }

  /** Reports a terminal venue state: any fill not yet reported, then canceled/rejected. */
  private async reportFinal(id: number, final: PlaceOrderResult, reported: Fill | undefined): Promise<void> {
    const fill = unreportedFill(reported, final);
    if (fill === null) {
      console.error(`order ${id}: venue fill totals cannot be reported exactly; leaving it to reconciliation`);
      this.ambiguous(id);
    } else if (fill) {
      await this.report(id, final.status === "filled" ? "filled" : "partially_filled", { ...final, ...fill });
    }
    if (final.status === "canceled") await this.report(id, "canceled", final);
    if (final.status === "rejected") await this.report(id, "rejected", final);
  }

  private async report(
    clientOrderId: number,
    event: ExecutionEvent,
    fields: { venue_order_id?: string; qty?: DecimalString; price?: DecimalString; reason?: string },
  ): Promise<void> {
    const isFill = event === "filled" || event === "partially_filled";
    const ack = await this.config.reporter.reportExecution({
      op: "report_execution",
      execution: {
        client_order_id: clientOrderId,
        event,
        qty: isFill ? fields.qty : undefined,
        price: isFill ? fields.price : undefined,
        venue_order_id: fields.venue_order_id === undefined ? undefined : bridgeVenueOrderId(fields.venue_order_id),
        reason: fields.reason,
        ts_ns: this.now(),
      },
    });
    if (!ack.ok) {
      // The order itself is real and already happened; a failed report is a books-integrity
      // problem for ops to chase, not a reason to hide the order's own outcome from the caller.
      console.error(`report_execution for client_order_id ${clientOrderId} was not ok:`, ack.error);
    }
  }
}
