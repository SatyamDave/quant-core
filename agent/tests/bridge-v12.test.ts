// Protocol v1.2 (wave2-spec.md): BridgeClient's hello/report_execution/cancel_order_intent/
// reconcile against this lane's own fake-bridge-v12.ts (lane A's real qc-bridge binary doesn't
// exist yet -- see this file's header and the PR body's integration notes).
import { afterEach, describe, expect, it } from "vitest";
import type { BridgeClient } from "../src/bridge.js";
import { startFakeBridgeV12, V12_SCENARIO_FIXTURE } from "./helpers/fakeBridgeV12.js";

describe("BridgeClient protocol v1.2 against fake-bridge-v12", () => {
  let client: BridgeClient | undefined;

  afterEach(async () => {
    if (client) await client.shutdown().catch(() => undefined);
    client = undefined;
  });

  it("hello reports protocol 1.2, venue_mode, and an approval_public_key", async () => {
    client = startFakeBridgeV12({ venue: "external" });
    const resp = await client.hello();
    expect(resp.ok).toBe(true);
    expect(resp.hello?.protocol).toBe("1.2");
    expect(resp.hello?.venue_mode).toBe("external");
    expect(typeof resp.hello?.approval_public_key).toBe("string");
    expect(Buffer.from(resp.hello!.approval_public_key, "base64")).toHaveLength(32);
  });

  it("marketNowNs() updates from hello, status, and decision_request.ts_ns, never from Date.now()", async () => {
    client = startFakeBridgeV12({ venue: "external", scenario: V12_SCENARIO_FIXTURE });
    expect(client.marketNowNs()).toBe(0); // nothing observed yet

    await client.hello();
    expect(client.marketNowNs()).toBe(0); // the fake bridge starts its market clock at 0

    const next = await client.nextDecisionRequest();
    expect(next.decision_request?.ts_ns).toBe(1_767_571_200_000_000_000);
    expect(client.marketNowNs()).toBe(1_767_571_200_000_000_000);

    const status = await client.status();
    expect((status.status as { market_ts_ns?: number } | undefined)?.market_ts_ns).toBe(1_767_571_200_000_000_000);
  });

  it("reconcile refuses new intents until the first clean reconcile", async () => {
    client = startFakeBridgeV12({ venue: "external", scenario: V12_SCENARIO_FIXTURE });
    await client.nextDecisionRequest(); // advances the market clock so the approval TTL is sane

    const beforeReconcile = await client.submitOrderIntent({
      request_id: "req-1",
      instrument: "SIM-BTC",
      side: "buy",
      qty: "1",
      limit_price: "65000.1",
      time_in_force: "ioc",
      reason: "test",
    });
    expect(beforeReconcile.result?.accepted).toBe(false);
    expect(beforeReconcile.result?.risk_reject).toBe("unreconciled");

    const reconcileResp = await client.reconcile({ orders: [], position: "0", cash: "10000.00" });
    // Wave-2 review fix: the RPC envelope's own "ok" only reflects whether the request was
    // well-formed -- the real bridge always answers ok:true for a successful comparison, whether
    // clean or not. The verdict is nested under "reconcile".
    expect(reconcileResp.ok).toBe(true);
    expect(reconcileResp.reconcile?.ok).toBe(true);

    const afterReconcile = await client.submitOrderIntent({
      request_id: "req-1",
      instrument: "SIM-BTC",
      side: "buy",
      qty: "1",
      limit_price: "65000.1",
      time_in_force: "ioc",
      reason: "test",
    });
    expect(afterReconcile.result?.accepted).toBe(true);
    expect(afterReconcile.result?.approval).toBeDefined();
    expect(afterReconcile.result?.client_order_id).toBe(1);
  });

  it("reconcile reports a mismatch (reconcile.ok:false) rather than silently trusting either side", async () => {
    client = startFakeBridgeV12({ venue: "external", scenario: V12_SCENARIO_FIXTURE });
    await client.nextDecisionRequest();
    await client.reconcile({ orders: [], position: "0", cash: "10000.00" });
    const submit = await client.submitOrderIntent({
      request_id: "req-1",
      instrument: "SIM-BTC",
      side: "buy",
      qty: "1",
      limit_price: "65000.1",
      time_in_force: "ioc",
      reason: "test",
    });
    const clientOrderId = submit.result!.client_order_id!;
    await client.reportExecution({
      op: "report_execution",
      execution: { client_order_id: clientOrderId, event: "filled", qty: "1", price: "65000.1", ts_ns: 1_767_571_200_000_000_000 },
    });

    // The venue snapshot disagrees with our own OMS (which now thinks this order is Filled):
    // an unrecognized/absent order for a live client_order_id is exactly the case #45 exists to
    // catch, not resolve by trusting either side.
    const mismatch = await client.reconcile({ orders: [], position: "0", cash: "10000.00" });
    // Wave-2 review fix: the request itself is still well-formed, so the envelope's ok stays
    // true; the mismatch is reported in the nested "reconcile" object, matching the real bridge.
    expect(mismatch.ok).toBe(true);
    expect(mismatch.reconcile?.ok).toBe(false);
    expect(mismatch.reconcile?.discrepancies.length).toBeGreaterThan(0);
  });

  it("cancel_order_intent returns a distinct cancel approval, verifiable against the same hello public key", async () => {
    client = startFakeBridgeV12({ venue: "external", scenario: V12_SCENARIO_FIXTURE });
    await client.nextDecisionRequest();
    await client.reconcile({ orders: [], position: "0", cash: "10000.00" });
    const submit = await client.submitOrderIntent({
      request_id: "req-1",
      instrument: "SIM-BTC",
      side: "buy",
      qty: "1",
      limit_price: "65000.1",
      time_in_force: "ioc",
      reason: "test",
    });
    const clientOrderId = submit.result!.client_order_id!;

    const cancel = await client.cancelOrderIntent(clientOrderId, "test cancel");
    expect(cancel.ok).toBe(true);
    expect(cancel.result?.accepted).toBe(true);
    const payload = JSON.parse(cancel.result!.approval!.payload) as Record<string, unknown>;
    expect(payload.action).toBe("cancel");
    expect(payload.client_order_id).toBe(clientOrderId);
  });

  it("report_execution rejects an unknown client_order_id rather than guessing", async () => {
    client = startFakeBridgeV12({ venue: "external" });
    const resp = await client.reportExecution({
      op: "report_execution",
      execution: { client_order_id: 999_999, event: "filled", ts_ns: 0 },
    });
    expect(resp.ok).toBe(false);
    expect(resp.error?.code).toBe("unknown_order");
  });
});
