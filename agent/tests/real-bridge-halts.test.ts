// Wave-2 integration verification: two of the coordinator's checklist items that
// run-external-sim.ts's happy path doesn't exercise (it never halts) -- both driven directly
// against the REAL qc-bridge binary via BridgeClient, independent of the full agent loop:
//
//   1. A reconcile mismatch halts the bridge (reason "reconciliation") and every subsequent
//      submit is refused, exactly like `qc_replay::Engine`'s own gate.
//   2. `--kill-file` halts the bridge and moves an open order to `pending_cancel`.
//
// What this does NOT (yet) prove, and says so rather than faking it: engine.rs's own comment on
// `cancel_all_open` says a kill-triggered external-mode cancel is "requested locally... the TS
// gateway... observes that through status and finishes the cancel for real" -- but no approval is
// minted for that cancel (only an explicit `cancel_order_intent` call gets one), and
// BrokerGateway.cancelOrder requires a signed CancelApprovalPayload. There is currently no code
// path that lets the gateway complete a kill-triggered cancel against the real venue with the
// same authorization bar every other order/cancel goes through. Flagged here rather than silently
// worked around; see this PR's integration notes.
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { BridgeClient } from "../src/bridge.js";
import { resolveRealBridgeBin } from "../src/testkit/real-bridge-bin.js";
import { writeRealBridgeFixtures } from "../src/testkit/real-bridge-fixtures.js";

describe("real qc-bridge, --venue external: halts", () => {
  const clients: BridgeClient[] = [];
  afterEach(async () => {
    await Promise.all(clients.splice(0).map((c) => c.shutdown().catch(() => undefined)));
  });

  function startBridge(extraArgs: string[] = []): { bridge: BridgeClient; fixturesDir: string } {
    const fixturesDir = mkdtempSync(path.join(tmpdir(), "real-bridge-halts-"));
    const fixtures = writeRealBridgeFixtures(fixturesDir);
    const bridge = new BridgeClient({
      command: resolveRealBridgeBin(),
      args: [
        fixtures.recordingPath,
        "--limits",
        fixtures.limitsPath,
        "--model",
        fixtures.modelPath,
        "--model-sha256",
        fixtures.modelSha256,
        "--decide-every",
        "1",
        "--venue",
        "external",
        ...extraArgs,
      ],
    });
    bridge.start();
    clients.push(bridge);
    return { bridge, fixturesDir };
  }

  it("a reconcile mismatch halts the bridge and a subsequent submit is refused", async () => {
    const { bridge } = startBridge();
    await bridge.nextDecisionRequest(); // sync the book

    const clean = await bridge.reconcile({ orders: [], position: "0", cash: null });
    expect(clean.ok).toBe(true);
    expect(clean.reconcile?.ok).toBe(true);

    // A venue order this process never opened: exactly the "unknown venue order" discrepancy
    // schemas/decision/v1/README.md's v1.2 section describes.
    const mismatch = await bridge.reconcile({
      orders: [{ client_order_id: null, venue_order_id: "999999", state: "open", filled_qty: "0" }],
      position: "0",
      cash: null,
    });
    expect(mismatch.ok).toBe(true); // the RPC itself is well-formed
    expect(mismatch.reconcile?.ok).toBe(false);
    expect(mismatch.reconcile?.discrepancies.length).toBeGreaterThan(0);

    const status = await bridge.status();
    expect((status.status as { halted?: string })?.halted).toBe("reconciliation");

    const submit = await bridge.submitOrderIntent({
      request_id: "after-mismatch",
      instrument: "1",
      side: "buy",
      qty: "1",
      limit_price: "100.02",
      time_in_force: "ioc",
      reason: "must be refused",
    });
    expect(submit.result?.accepted).toBe(false);
    expect(submit.result?.halted).toBe("reconciliation");
  }, 20_000);

  it("--kill-file halts the bridge within its polling and cancels the open order (visible via status)", async () => {
    const killFile = path.join(mkdtempSync(path.join(tmpdir(), "real-bridge-killfile-")), "kill");
    const { bridge } = startBridge(["--kill-file", killFile]);
    await bridge.nextDecisionRequest();

    const clean = await bridge.reconcile({ orders: [], position: "0", cash: null });
    expect(clean.reconcile?.ok).toBe(true);

    const submit = await bridge.submitOrderIntent({
      request_id: "before-kill",
      instrument: "1",
      side: "buy",
      qty: "1",
      limit_price: "100.02",
      time_in_force: "ioc",
      reason: "resting order the kill must cancel",
    });
    expect(submit.result?.accepted).toBe(true);

    writeFileSync(killFile, "");
    // Any op polls the kill file (wire.rs: "polled on every record and op"); `status` is the
    // harmless one a real heartbeat would use.
    const afterKill = await bridge.status();
    expect((afterKill.status as { halted?: string })?.halted).toBe("kill_switch");
    const openOrders = (afterKill.status as { open_orders?: Array<{ state: string }> })?.open_orders ?? [];
    expect(openOrders).toHaveLength(1);
    expect(openOrders[0]?.state).toBe("pending_cancel");

    const rejected = await bridge.submitOrderIntent({
      request_id: "after-kill",
      instrument: "1",
      side: "buy",
      qty: "1",
      limit_price: "100.02",
      time_in_force: "ioc",
      reason: "must be refused",
    });
    expect(rejected.result?.accepted).toBe(false);
    expect(rejected.result?.halted).toBe("kill_switch");
  }, 20_000);
});
