import { afterEach, describe, expect, it } from "vitest";
import { BridgeClient } from "../src/bridge.js";
import { SCENARIO_FIXTURE, startFakeBridge } from "./helpers/fakeBridge.js";

describe("BridgeClient against a real child-process fake bridge", () => {
  let client: BridgeClient | undefined;

  afterEach(async () => {
    if (client) await client.shutdown().catch(() => undefined);
    client = undefined;
  });

  it("matches responses by id, not by arrival order", async () => {
    client = startFakeBridge({ mode: "out_of_order", scenario: SCENARIO_FIXTURE });
    const [first, second] = await Promise.all([
      client.nextDecisionRequest(),
      client.nextDecisionRequest(),
    ]);
    // The fake bridge answers the second request first; the client must still hand each
    // caller the response for the id it actually asked for.
    expect(first.ok).toBe(true);
    expect(first.decision_request?.request_id).toBe("req-1");
    expect(second.ok).toBe(true);
    expect(second.decision_request?.request_id).toBe("req-2");
  });

  it("resolves (not rejects) an ok:false error response", async () => {
    client = startFakeBridge({ mode: "error_next_decision_request" });
    const resp = await client.nextDecisionRequest();
    expect(resp.ok).toBe(false);
    expect(resp.error?.code).toBe("bad_request");
  });

  it("rejects with a timeout when the bridge never responds", async () => {
    client = startFakeBridge({ mode: "timeout_next_decision_request", timeoutMs: 100 });
    await expect(client.nextDecisionRequest()).rejects.toThrow(/timed out/);
  });

  it("rejects the pending call on an unparseable line from the bridge", async () => {
    client = startFakeBridge({ mode: "malformed_json" });
    await expect(client.nextDecisionRequest()).rejects.toThrow(/non-JSON/);
  });

  it("rejects the pending call on a response that fails schema validation", async () => {
    client = startFakeBridge({ mode: "invalid_schema" });
    await expect(client.nextDecisionRequest()).rejects.toThrow(/schema validation failed/);
  });

  it("throws when sending before start()", async () => {
    const unstarted = new BridgeClient({ command: "true" });
    await expect(unstarted.nextDecisionRequest()).rejects.toThrow(/start\(\) was not called/);
  });

  it("runs the real qc-bridge binary when QC_BRIDGE_BIN is set", async () => {
    const bin = process.env.QC_BRIDGE_BIN;
    if (!bin) {
      // Optional integration test: the Rust binary is built on a separate lane (agentic/bridge)
      // and isn't guaranteed to exist in this worktree. Skip cleanly instead of failing.
      return;
    }
    const real = new BridgeClient({ command: bin });
    real.start();
    const resp = await real.status();
    expect(resp.ok).toBe(true);
    await real.shutdown();
  });
});
