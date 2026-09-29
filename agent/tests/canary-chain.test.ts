// The live canary's chain end to end against mocks (src/testkit/run-canary-mock.ts): the real
// qc-bridge in --follow --venue external mode on a generated instrument file, the OpenRouter
// decider against a mock model server, and the gateway against the mock broker.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { runCanaryMock } from "../src/testkit/run-canary-mock.js";

const lines = (file: string) =>
  readFileSync(file, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l) as Record<string, unknown>);

describe("live canary chain (mocks only)", () => {
  it("trades one approved order to a fill, then the kill file halts and cancels the resting one", async () => {
    const r = await runCanaryMock();

    const ledger = lines(r.ledgerPath);
    expect(ledger.map((e) => (e.decision as { action: string }).action)).toEqual(["buy", "buy"]);
    for (const e of ledger) {
      expect(e.model).toBe("openrouter:mock/model:free");
      expect((e.result as { accepted: boolean; approval?: unknown }).accepted).toBe(true);
      expect((e.result as { approval?: unknown }).approval).toBeDefined();
    }
    const resolved = lines(r.journalPath)
      .filter((j) => j.phase === "resolved")
      .map((j) => [j.client_order_id, (j.result as { status: string }).status]);
    expect(resolved).toEqual([
      [1, "filled"],
      [2, "accepted"],
      [2, "canceled"],
    ]);
    expect(r.placeCalls).toBe(2);
    expect(r.cancelCalls).toBe(1);
    expect(r.filledPosition).toBe("1.00000000"); // report_execution applied the fill
    expect(r.haltedReason).toBe("kill_switch");
    expect(r.openOrdersAfterKill).toBe(0);
  }, 60_000);
});
