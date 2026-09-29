// Issue #44 reopened ("Stop everything even if the agent itself is stuck"): the two safety gaps
// the security review found, each proved end to end against the REAL qc-bridge binary and the mock
// broker:
//
//   1. A hung (alive but stuck) decision loop never calls the bridge again -- so its own kill-file
//      polling (engine/crates/bridge/src/wire.rs's per-op check) would never fire if nothing else
//      called it either. agent/src/broker/heartbeat.ts's Heartbeat is the fix: a real wall-clock
//      timer, independent of runLoop's own await chain, that keeps polling `status` regardless.
//      This test hangs the decider on purpose and proves the heartbeat still observes the halt
//      within budget, printing the measured latency.
//   2. A halt in external mode moves every open order to PendingCancel at the bridge but (before
//      this PR) minted no approval for it, so the gateway had no signed authorization to actually
//      cancel it at the broker -- real-bridge-halts.test.ts's own header comment flagged this
//      exact gap. This test lets an order rest at the mock broker (an emulated IOC with a long
//      rest window, so only the halt can end it), kills the bridge, and proves the heartbeat
//      drains protocol v1.2.1's `pending_cancels` and finishes the cancel at the mock broker,
//      exactly once, deterministically across two independent runs.
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { BridgeClient } from "../src/bridge.js";
import { createExternalModeGateway } from "../src/broker/external-mode.js";
import { journalSha256RedactingSessions } from "../src/broker/journal.js";
import { FakeDecider } from "../src/decider/fake.js";
import type { Decider, DecisionOutcome } from "../src/decider/types.js";
import { ledgerSha256RedactingApprovalSignatures } from "../src/ledger.js";
import { runLoop } from "../src/loop.js";
import { resolveRealBridgeBin } from "../src/testkit/real-bridge-bin.js";
import { writeRealBridgeFixtures } from "../src/testkit/real-bridge-fixtures.js";
import { MockBroker } from "../src/testkit/mock-broker.js";

/** A decider whose decide() call never resolves -- standing in for a live LLM call that hangs,
 *  or any other stuck decision loop. Deliberately not awaited by its caller in either test below:
 *  the whole point is that nothing downstream of it ever gets a chance to run again. */
class HungDecider implements Decider {
  decide(): Promise<DecisionOutcome> {
    return new Promise<DecisionOutcome>(() => undefined);
  }
}

interface Harness {
  bridge: BridgeClient;
  broker: MockBroker;
  killFile: string;
  cancelCalls: () => number;
  stop: () => Promise<void>;
}

/** Spawns the real qc-bridge (--venue external --kill-file, decide_every=1) plus the mock
 *  broker, and wires createExternalModeGateway on top -- the same three pieces
 *  run-external-sim.ts already assembles for `just agent-sim-external`, with a kill file and a
 *  fast heartbeat interval added for these tests. */
async function startHarness(
  dir: string,
  opts: { journalPath: string; scenarios?: Record<string, "accepted">; iocCancelAfterMs?: number },
): Promise<Harness & { external: Awaited<ReturnType<typeof createExternalModeGateway>> }> {
  const fixtures = writeRealBridgeFixtures(path.join(dir, "fixtures"));
  const killFile = path.join(dir, "kill");
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
      "--kill-file",
      killFile,
    ],
  });
  bridge.start();

  let cancelCalls = 0;
  const broker = new MockBroker({
    scenarios: opts.scenarios,
    cash: "10000.00",
    onCall: ({ method }) => {
      if (method === "cancelOrder") cancelCalls += 1;
    },
  });

  const external = await createExternalModeGateway({
    bridge,
    broker,
    assetClass: "equity",
    instrument: "1",
    journalPath: opts.journalPath,
    iocCancelAfterMs: opts.iocCancelAfterMs,
    heartbeatIntervalMs: 25,
  });

  return {
    bridge,
    broker,
    killFile,
    cancelCalls: () => cancelCalls,
    external,
    stop: async () => {
      external.heartbeat.stop();
      await bridge.shutdown();
    },
  };
}

describe("heartbeat observes a halt even when the decision loop is stuck (issue #44 reopened)", () => {
  it("reports halted within budget while decider.decide() never returns, and measures the latency", async () => {
    const dir = mkdtempSync(path.join(tmpdir(), "heartbeat-hang-"));
    const journalPath = path.join(dir, "broker-journal.jsonl");
    const harness = await startHarness(dir, { journalPath });

    try {
      // Fire-and-forget on purpose: HungDecider never resolves, so this promise never settles.
      // The point of this test is that nothing about it being stuck matters to the heartbeat.
      void runLoop({
        bridge: harness.bridge,
        decider: new HungDecider(),
        mode: "fake",
        promptVersion: "v1",
        model: "fake-rule-v1",
        ledgerPath: path.join(dir, "ledger.jsonl"),
        external: { gateway: harness.external.gateway, assetClass: "equity", reconcile: harness.external.reconcile },
      });

      // Give the (now permanently stuck) loop a moment to actually reach the hang -- proves this
      // isn't just "the heartbeat won the race before the loop did anything at all".
      await new Promise((resolve) => setTimeout(resolve, 100));

      harness.external.heartbeat.start();
      const touchedAt = Date.now();
      writeFileSync(harness.killFile, "");

      const deadline = Date.now() + 5_000;
      while (!harness.external.heartbeat.isHalted && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
      const latencyMs = Date.now() - touchedAt;
      console.log(`heartbeat observed halted ${latencyMs}ms after the kill file was written`);

      expect(harness.external.heartbeat.isHalted).toBe(true);
      // CLAUDE.md rule 11's one-second budget, from the moment the file appeared to the
      // heartbeat's own status() call reporting it -- not from process start.
      expect(latencyMs).toBeLessThan(1_000);
    } finally {
      await harness.stop();
    }
  }, 20_000);
});

describe("heartbeat finishes halt-triggered cancels at the real broker (issue #44 reopened)", () => {
  async function runKillScenario(label: string): Promise<{ ledgerSha256: string; journalSha256: string; cancelCalls: number }> {
    const dir = mkdtempSync(path.join(tmpdir(), `heartbeat-kill-${label}-`));
    const ledgerPath = path.join(dir, "ledger.jsonl");
    const journalPath = path.join(dir, "broker-journal.jsonl");
    const harness = await startHarness(dir, {
      journalPath,
      // The first order rests unfilled; its emulated-IOC rest is far longer than the test, so
      // only the halt's cancel can end it.
      scenarios: { "1": "accepted" },
      iocCancelAfterMs: 600_000,
    });

    try {
      harness.external.heartbeat.start();
      const loop = runLoop({
        bridge: harness.bridge,
        decider: new FakeDecider({ probThreshold: 0.5 }),
        mode: "fake",
        promptVersion: "v1",
        model: "fake-rule-v1",
        ledgerPath,
        external: {
          gateway: harness.external.gateway,
          assetClass: "equity",
          reconcile: harness.external.reconcile,
          reconcileEveryNDecisions: 1_000, // only the startup reconcile matters for this test
        },
      }).catch(() => undefined); // after the halt the loop may stop on a refused decision

      const restingBy = Date.now() + 5_000;
      while (Date.now() < restingBy && ((await harness.bridge.status()).status?.open_orders ?? []).length === 0) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      writeFileSync(harness.killFile, "");

      const deadline = Date.now() + 5_000;
      let allClosed = false;
      while (Date.now() < deadline) {
        const status = await harness.bridge.status();
        const open = status.status?.open_orders ?? [];
        if (status.status?.halted && open.length === 0) {
          allClosed = true;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
      expect(allClosed).toBe(true);
      await loop;

      return {
        ledgerSha256: ledgerSha256RedactingApprovalSignatures(ledgerPath),
        journalSha256: journalSha256RedactingSessions(journalPath),
        cancelCalls: harness.cancelCalls(),
      };
    } finally {
      await harness.stop();
    }
  }

  it("cancels the resting order at the broker within the timeout, exactly once, deterministically across two runs", async () => {
    const first = await runKillScenario("1");
    const second = await runKillScenario("2");

    expect(first.cancelCalls).toBe(1);
    expect(second.cancelCalls).toBe(1);
    expect(second.ledgerSha256).toBe(first.ledgerSha256);
    expect(second.journalSha256).toBe(first.journalSha256);
  }, 30_000);
});
