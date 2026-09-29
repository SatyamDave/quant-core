// The door orders must pass through to reach the broker (#35), against the synthetic mock broker
// (agent/src/testkit/mock-broker.ts). Covers: happy
// path, tampered/expired approvals refused, duplicate client_order_id not resent, an ambiguous
// submit looked up by ref_id before a bounded resend with the same ref_id, crash-after-send
// recovery from the journal, IOC emulation (gfd + cancel after the window, final state
// reported), cancel failures sent to reconciliation, and only limit orders sent.
import { generateKeyPairSync, sign as cryptoSign } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { BrokerGateway, GatewayRejection, iocCancelAfterMsFromEnv, MAX_SENDS } from "../../src/broker/gateway.js";
import { openJournal, replayJournal } from "../../src/broker/journal.js";
import { refIdForApproval } from "../../src/broker/adapter.js";
import { ed25519PublicKeyFromRaw } from "../../src/broker/crypto.js";
import type {
  Approval,
  ApprovalPayload,
  ApprovedOrder,
  BridgeReporter,
  CancelApprovalPayload,
  ReportExecutionOp,
} from "../../src/broker/types.js";
import { MockBroker, type MockScenario } from "../../src/testkit/mock-broker.js";

function tempJournalPath(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "broker-gateway-journal-"));
  return path.join(dir, "broker-journal.jsonl");
}

// --- a tiny, local fake bridge reporter for THIS lane's tests only. Not
// agent/src/testkit/fake-bridge.ts (that fakes qc-bridge's v1 JSON-Lines process, and is a
// different lane's shared file); this just records report_execution calls in memory. ---
class RecordingReporter implements BridgeReporter {
  readonly calls: ReportExecutionOp[] = [];
  ok = true;
  async reportExecution(op: ReportExecutionOp) {
    this.calls.push(op);
    return this.ok ? { ok: true as const } : { ok: false as const, error: { code: "boom", message: "synthetic failure" } };
  }
}

const { publicKey, privateKey } = generateKeyPairSync("ed25519");
const rawPublicKey = (publicKey.export({ type: "spki", format: "der" }) as Buffer).subarray(-32);
const SESSION = rawPublicKey.toString("base64");

const NOW_NS = 1_767_571_200_000_000_000; // arbitrary fixed instant

function canonicalPayload(fields: ApprovalPayload): string {
  // Mirrors the bridge's canonicalization: sorted keys, no whitespace. Order matters for the
  // signature to be reproducible the same way every time, not for correctness of what's
  // compared (gateway.ts re-parses and compares fields, never string-diffs the JSON).
  const sorted: ApprovalPayload = {
    client_order_id: fields.client_order_id,
    expires_ts_ns: fields.expires_ts_ns,
    instrument: fields.instrument,
    limit_price: fields.limit_price,
    qty: fields.qty,
    request_id: fields.request_id,
    side: fields.side,
    time_in_force: fields.time_in_force,
  };
  return JSON.stringify(sorted);
}

function approve(fields: ApprovalPayload): Approval {
  const payload = canonicalPayload(fields);
  const signature = cryptoSign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64");
  return { payload, signature };
}

function canonicalCancelPayload(fields: CancelApprovalPayload): string {
  // Sorted keys, no whitespace -- same canonicalization convention as canonicalPayload() above,
  // for protocol v1.2's distinct (smaller) cancel approval payload shape.
  const sorted: CancelApprovalPayload = {
    action: fields.action,
    client_order_id: fields.client_order_id,
    expires_ts_ns: fields.expires_ts_ns,
  };
  return JSON.stringify(sorted);
}

function approveCancel(fields: CancelApprovalPayload): Approval {
  const payload = canonicalCancelPayload(fields);
  const signature = cryptoSign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64");
  return { payload, signature };
}

function cancelFields(clientOrderId: number): CancelApprovalPayload {
  return { action: "cancel", client_order_id: clientOrderId, expires_ts_ns: NOW_NS + 5_000_000_000 };
}

function orderFrom(fields: ApprovalPayload): ApprovedOrder {
  return {
    client_order_id: fields.client_order_id,
    instrument: fields.instrument,
    side: fields.side,
    qty: fields.qty,
    limit_price: fields.limit_price,
    time_in_force: fields.time_in_force,
    request_id: fields.request_id,
  };
}

function baseFields(clientOrderId: number): ApprovalPayload {
  return {
    client_order_id: clientOrderId,
    expires_ts_ns: NOW_NS + 5_000_000_000, // +5s TTL, per protocol v1.1
    instrument: "SPY",
    // $20 notional: inside the $25/order max_notional (config/limits, #106).
    limit_price: "20.00",
    qty: "1",
    request_id: `req-${clientOrderId}`,
    side: "buy",
    time_in_force: "ioc",
  };
}

describe("BrokerGateway", () => {
  let reporter: RecordingReporter;

  beforeEach(() => {
    reporter = new RecordingReporter();
  });

  type Call = { name: string; args: Record<string, unknown>; at: number };

  interface Harness {
    gateway: BrokerGateway;
    broker: MockBroker;
    calls: Call[];
    ambiguous: number[];
    journalPath: string;
    clock: { ns: number };
  }

  // Every test gets its own mock broker and journal file. Scenario keys are placement order
  // ("1" = the first distinct ref_id placed), not client_order_id.
  async function harness(scenarios: Record<string, MockScenario>, opts: { iocCancelAfterMs?: number; journalPath?: string } = {}): Promise<Harness> {
    const calls: Call[] = [];
    const broker = new MockBroker({ scenarios, onCall: (c) => calls.push({ name: c.method, args: c.args, at: c.at }) });
    const ambiguous: number[] = [];
    const clock = { ns: NOW_NS };
    const journalPath = opts.journalPath ?? tempJournalPath();
    const gateway = new BrokerGateway({
      broker,
      reporter,
      approvalPublicKey: ed25519PublicKeyFromRaw(rawPublicKey),
      now: () => clock.ns,
      requestTimeoutMs: 100,
      iocCancelAfterMs: opts.iocCancelAfterMs ?? 20,
      journalPath,
      onAmbiguousOutcome: (id) => ambiguous.push(id),
    });
    return { gateway, broker, calls, ambiguous, journalPath, clock };
  }

  const places = (calls: Call[]) => calls.filter((c) => c.name === "placeOrder");
  const placedOrder = (c: Call) => c.args.order as ApprovedOrder;
  const events = () => reporter.calls.map((c) => c.execution.event);

  it("happy path: a validly approved order is placed and the fill is reported back", async () => {
    const fields = baseFields(1);
    const { gateway } = await harness({ "1": "filled" });

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(result.status).toBe("filled");
    expect(reporter.calls).toHaveLength(1);
    expect(reporter.calls[0]!.execution).toMatchObject({ client_order_id: 1, event: "filled", qty: "1", price: "20" });
  });

  it("refuses a tampered approval (order changed after approval) before any network call", async () => {
    const fields = baseFields(2);
    const { gateway, calls } = await harness({ "1": "filled" });
    const tamperedOrder = orderFrom({ ...fields, qty: "2" });

    await expect(gateway.submitOrder(tamperedOrder, approve(fields), "equity")).rejects.toThrow(GatewayRejection);
    expect(calls).toEqual([]);
    expect(reporter.calls).toHaveLength(0);
  });

  it("refuses an approval whose payload bytes were edited (signature no longer verifies)", async () => {
    const fields = baseFields(3);
    const { gateway } = await harness({ "1": "filled" });
    const approval = approve(fields);
    const edited: Approval = { ...approval, payload: canonicalPayload({ ...fields, qty: "2" }) };

    await expect(gateway.submitOrder(orderFrom(fields), edited, "equity")).rejects.toThrow(/signature/);
  });

  it("refuses a missing approval", async () => {
    const fields = baseFields(4);
    const { gateway } = await harness({});
    await expect(gateway.submitOrder(orderFrom(fields), undefined, "equity")).rejects.toThrow(/missing approval/);
  });

  it("refuses an expired approval", async () => {
    const expired = { ...baseFields(5), expires_ts_ns: NOW_NS - 1 };
    const { gateway } = await harness({ "1": "filled" });

    await expect(gateway.submitOrder(orderFrom(expired), approve(expired), "equity")).rejects.toThrow(/expired/);
  });

  it("refuses a fractional-share approval before any call: only whole-share limit orders are sent", async () => {
    const fields = { ...baseFields(6), qty: "0.5" };
    const { gateway, calls } = await harness({ "1": "filled" });

    await expect(gateway.submitOrder(orderFrom(fields), approve(fields), "equity")).rejects.toThrow(/whole number of shares/);
    expect(calls).toEqual([]);
  });

  it("sends only the approved limit order: every place and preview carries its limit price and time in force", async () => {
    const { gateway, calls } = await harness({ "1": "filled", "2": "accepted" });
    const ioc = baseFields(7);
    const gtc = { ...baseFields(8), time_in_force: "gtc" as const };
    await gateway.submitOrder(orderFrom(ioc), approve(ioc), "equity");
    await gateway.submitOrder(orderFrom(gtc), approve(gtc), "equity");

    const orderCalls = calls.filter((c) => c.name === "placeOrder" || c.name === "previewOrder");
    expect(orderCalls).toHaveLength(4);
    for (const c of orderCalls) expect(placedOrder(c).limit_price).toBe("20.00");
    expect(places(calls).map((c) => placedOrder(c).time_in_force)).toEqual(["ioc", "gtc"]);
  });

  it("never resends a duplicate client_order_id once it has a terminal outcome", async () => {
    const fields = baseFields(9);
    const { gateway, calls } = await harness({ "1": "filled" });

    const first = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");
    const second = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(second).toEqual(first);
    expect(places(calls)).toHaveLength(1);
    expect(reporter.calls).toHaveLength(1);
  });

  it("ambiguous submit (response lost): looks the order up by ref_id before anything else, finds it, never places twice", async () => {
    const fields = baseFields(10);
    const { gateway, calls } = await harness({ "1": "timeout" });

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(result.status).toBe("filled");
    expect(calls.map((c) => c.name)).toEqual(["previewOrder", "placeOrder", "findOrderByRefId"]);
    expect(events()).toEqual(["filled"]);
  });

  it("ambiguous submit that never arrived: queries first, then resends with the SAME ref_id, derived from the signed approval", async () => {
    const fields = baseFields(11);
    const approval = approve(fields);
    const { gateway, calls, journalPath } = await harness({ "1": "lost_once" });

    const result = await gateway.submitOrder(orderFrom(fields), approval, "equity");

    expect(result.status).toBe("filled");
    const names = calls.map((c) => c.name).filter((n) => n !== "previewOrder");
    expect(names).toEqual(["placeOrder", "findOrderByRefId", "placeOrder"]);
    const refIds = places(calls).map((c) => c.args.refId);
    expect(refIds).toEqual([refIdForApproval(approval.payload), refIdForApproval(approval.payload)]);
    // Both attempts were journaled (fsync'd) under that ref_id before being sent.
    expect(replayJournal(journalPath, SESSION).orders.get(11)).toMatchObject({ state: "terminal", refId: refIds[0] });
  });

  it(`resends at most ${MAX_SENDS} times in all, then hands the order to reconciliation`, async () => {
    const fields = baseFields(12);
    const { gateway, calls, ambiguous, journalPath } = await harness({ "1": "never_arrives" });

    await expect(gateway.submitOrder(orderFrom(fields), approve(fields), "equity")).rejects.toThrow(/outcome unknown/);

    expect(places(calls)).toHaveLength(MAX_SENDS);
    expect(new Set(places(calls).map((c) => c.args.refId)).size).toBe(1);
    expect(ambiguous).toEqual([12]);
    expect(replayJournal(journalPath, SESSION).orders.get(12)).toMatchObject({ state: "unknown", sends: MAX_SENDS });
    expect(reporter.calls).toHaveLength(0);
  });

  it("does not resend once the approval has expired, even when the lookup found nothing", async () => {
    const fields = baseFields(13);
    const h = await harness({ "1": "never_arrives" });
    const timedOut = h.gateway.submitOrder(orderFrom(fields), approve(fields), "equity");
    while (places(h.calls).length === 0) await new Promise((r) => setTimeout(r, 1));
    h.clock.ns = fields.expires_ts_ns; // the approval expires while the first send hangs

    await expect(timedOut).rejects.toThrow(/approval has expired; not sending again/);
    expect(places(h.calls)).toHaveLength(1);
    expect(h.ambiguous).toEqual([13]);
  });

  it("crash-after-send: a restarted gateway finds the order by the journaled ref_id instead of placing it again", async () => {
    const fields = baseFields(14);
    const approval = approve(fields);
    const refId = refIdForApproval(approval.payload);
    const journalPath = tempJournalPath();
    const h = await harness({ "1": "filled" }, { journalPath });
    // The process before the crash journaled the attempt, and the broker received the order.
    openJournal(journalPath, SESSION).recordAttempt(14, refId, "2026-09-29T14:30:00.000Z");
    await h.broker.placeOrder("equity", orderFrom(fields), refId);
    const restarted = new BrokerGateway({
      broker: h.broker,
      reporter,
      approvalPublicKey: ed25519PublicKeyFromRaw(rawPublicKey),
      now: () => NOW_NS,
      requestTimeoutMs: 100,
      journalPath,
    });
    h.calls.length = 0;

    const resolved = await restarted.submitOrder(orderFrom(fields), approval, "equity");

    expect(resolved.status).toBe("filled");
    expect(h.calls.map((c) => c.name)).toEqual(["findOrderByRefId"]);
    expect(events()).toEqual(["filled"]);
  });

  it("emulated IOC: rests, is cancelled by the gateway after the window, and the final state is read back and reported", async () => {
    const fields = baseFields(15);
    const { gateway, calls, journalPath } = await harness({ "1": "accepted" }, { iocCancelAfterMs: 60 });

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(result.status).toBe("canceled");
    const placed = places(calls)[0]!;
    const cancel = calls.find((c) => c.name === "cancelOrder")!;
    expect(cancel.at - placed.at).toBeGreaterThanOrEqual(55);
    expect(calls.at(-1)!.name).toBe("getOrderById"); // the state reported is the one read back
    expect(events()).toEqual(["accepted", "canceled"]);
    expect(reporter.calls[0]!.execution.venue_order_id).toMatch(/^[0-9]+$/);
    expect(replayJournal(journalPath, SESSION).orders.get(15)).toMatchObject({ state: "terminal", result: { status: "canceled" } });
  });

  it("emulated IOC journals the venue order id before resting, so a restart can still cancel it", async () => {
    const fields = baseFields(16);
    const { gateway, journalPath } = await harness({ "1": "accepted" }, { iocCancelAfterMs: 200 });

    const pending = gateway.submitOrder(orderFrom(fields), approve(fields), "equity");
    await new Promise((r) => setTimeout(r, 100));
    const midRest = replayJournal(journalPath, SESSION).orders.get(16);
    await pending;

    expect(midRest).toMatchObject({ state: "terminal", result: { status: "accepted", venue_order_id: expect.stringMatching(/-/) } });
  });

  it("emulated IOC with a partial fill reports the fill, then the cancel of the rest", async () => {
    const fields = { ...baseFields(17), qty: "1" };
    const { gateway } = await harness({ "1": "partially_filled" });

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(result).toMatchObject({ status: "canceled", filled_qty: "0.5" });
    expect(events()).toEqual(["accepted", "partially_filled", "canceled"]);
  });

  it("emulated IOC whose cancel fails because it filled first: reports the fill read back, never assumes a cancel", async () => {
    const fields = baseFields(18);
    const { gateway, ambiguous } = await harness({ "1": "cancel_fails_filled" });

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(result.status).toBe("filled");
    expect(events()).toEqual(["accepted", "filled"]);
    expect(ambiguous).toEqual([]);
  });

  it("emulated IOC whose cancel never takes: reconciles, reports no cancel, and leaves the order cancellable", async () => {
    const fields = baseFields(19);
    const { gateway, ambiguous } = await harness({ "1": "cancel_stuck" });

    await expect(gateway.submitOrder(orderFrom(fields), approve(fields), "equity")).rejects.toThrow(/cancel not confirmed/);

    expect(ambiguous).toEqual([19]);
    expect(events()).toEqual(["accepted"]);
    // Still known as live with its venue id: a halt's approved cancel reaches it.
    await expect(gateway.cancelOrder(19, fields.instrument, "equity", approveCancel(cancelFields(19)))).resolves.toMatchObject({
      status: "cancel_rejected",
    });
  }, 10_000);

  it("a halt's cancel ends an emulated IOC order's rest early", async () => {
    const fields = baseFields(20);
    const { gateway } = await harness({ "1": "accepted" }, { iocCancelAfterMs: 60_000 });

    const pending = gateway.submitOrder(orderFrom(fields), approve(fields), "equity");
    while (!events().includes("accepted")) await new Promise((r) => setTimeout(r, 5));
    const cancel = await gateway.cancelOrder(20, fields.instrument, "equity", approveCancel(cancelFields(20)));

    expect(cancel.status).toBe("canceled");
    expect((await pending).status).toBe("canceled");
    expect(events()).toEqual(["accepted", "canceled"]);
  });

  it("reports an explicit broker rejection back to the bridge as rejected", async () => {
    const fields = baseFields(21);
    const { gateway } = await harness({ "1": "rejected" });

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(result.status).toBe("rejected");
    expect(reporter.calls).toHaveLength(1);
    expect(reporter.calls[0]!.execution).toMatchObject({ client_order_id: 21, event: "rejected" });
    expect(reporter.calls[0]!.execution.reason).toMatch(/SYNTHETIC/);
  });

  it("cancels a resting gtc order by its journaled venue order id and reports the cancellation", async () => {
    const fields = { ...baseFields(22), time_in_force: "gtc" as const };
    const { gateway, calls } = await harness({ "1": "accepted" });
    const placed = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    const result = await gateway.cancelOrder(22, fields.instrument, "equity", approveCancel(cancelFields(22)));

    expect(result.status).toBe("canceled");
    expect(calls.find((c) => c.name === "cancelOrder")!.args).toEqual({ assetClass: "equity", venueOrderId: placed.venue_order_id });
    expect(events()).toEqual(["accepted", "canceled"]);
  });

  it("refuses to cancel an order it never placed, without calling the broker", async () => {
    const { gateway, calls } = await harness({});
    await expect(gateway.cancelOrder(12345, "SPY", "equity", approveCancel(cancelFields(12345)))).rejects.toThrow(GatewayRejection);
    expect(calls).toEqual([]);
  });

  it("refuses a cancel with a missing, mismatched or expired approval", async () => {
    const fields = { ...baseFields(23), time_in_force: "gtc" as const };
    const { gateway } = await harness({ "1": "accepted" });
    await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    await expect(gateway.cancelOrder(23, fields.instrument, "equity", undefined)).rejects.toThrow(/missing approval/);
    await expect(gateway.cancelOrder(23, fields.instrument, "equity", approveCancel(cancelFields(999)))).rejects.toThrow(/does not match/);
    const expired: CancelApprovalPayload = { ...cancelFields(23), expires_ts_ns: NOW_NS - 1 };
    await expect(gateway.cancelOrder(23, fields.instrument, "equity", approveCancel(expired))).rejects.toThrow(/expired/);
  });

  it("still returns the order's own outcome even if reporting the execution back fails", async () => {
    const fields = baseFields(24);
    const { gateway } = await harness({ "1": "filled" });
    reporter.ok = false;

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");

    expect(result.status).toBe("filled");
    expect(reporter.calls).toHaveLength(1);
  });
});

describe("QC_IOC_CANCEL_AFTER_MS", () => {
  it("defaults when unset and refuses anything outside 1..60000 ms", () => {
    expect(iocCancelAfterMsFromEnv(undefined)).toBeUndefined();
    expect(iocCancelAfterMsFromEnv("2000")).toBe(2000);
    for (const bad of ["", "0", "60001", "1.5", "-1", "2s"]) expect(() => iocCancelAfterMsFromEnv(bad)).toThrow(/1 to 60000/);
  });
});
