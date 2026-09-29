import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { BridgeClient } from "../src/bridge.js";
import { FakeDecider } from "../src/decider/fake.js";
import { ledgerSha256 } from "../src/ledger.js";
import { runLoop } from "../src/loop.js";
import { SCENARIO_FIXTURE, startFakeBridge } from "./helpers/fakeBridge.js";

describe("runLoop against the fake bridge (offline, deterministic)", () => {
  const bridges: BridgeClient[] = [];

  afterEach(async () => {
    await Promise.all(bridges.splice(0).map((b) => b.shutdown().catch(() => undefined)));
  });

  async function runOnce(): Promise<{ hash: string; decisions: number; noTrades: number }> {
    const dir = mkdtempSync(path.join(tmpdir(), "agent-loop-"));
    const ledgerPath = path.join(dir, "ledger.jsonl");
    const bridge = startFakeBridge({ scenario: SCENARIO_FIXTURE });
    bridges.push(bridge);

    const counts = await runLoop({
      bridge,
      decider: new FakeDecider(),
      mode: "fake",
      promptVersion: "v1",
      model: "fake-rule-v1",
      ledgerPath,
    });

    return { hash: ledgerSha256(ledgerPath), decisions: counts.decisions, noTrades: counts.noTrades };
  }

  it("consumes the whole feed and stops when the bridge returns decision_request: null", async () => {
    const { decisions, noTrades } = await runOnce();
    // scenario.json: req-1 signals up with high confidence (a trade), req-2 signals flat (no_trade).
    expect(decisions).toBe(2);
    expect(noTrades).toBe(1);
  });

  it("produces an identical ledger sha256 across two independent runs of the same feed", async () => {
    const first = await runOnce();
    const second = await runOnce();
    expect(first.hash).toBe(second.hash);
    expect(first.hash).toMatch(/^[0-9a-f]{64}$/);
  });
});
