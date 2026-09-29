#!/usr/bin/env node
// `just agent-sim-external` (wave2-spec.md, issue #45 + gateway-integration's wave2 scope):
// agent (fake decider) -> the REAL qc-bridge --venue external -> gateway -> mock broker ->
// report_execution, exercising at least one partial fill, one reject, and one cancel, and
// proving two independent runs produce identical ledger and broker-journal hashes.
//
// This is a testkit script, not agent/src/cli.ts: test/demo wiring lives in this directory
// (fake-bridge.ts, fake-bridge-v12.ts, mock-broker.ts), not in cli.ts. This file wires the exact
// same production pieces (BridgeClient, createExternalModeGateway, runLoop) to the mock broker.
//
// Wave-2 integration: now spawns the REAL qc-bridge binary (built from wave2/bridge-control,
// protocol v1.2's `hello`/`cancel_order_intent`/`reconcile`/`--venue external`) via
// real-bridge-bin.ts/real-bridge-fixtures.ts, replacing this lane's own fake-bridge-v12.ts here
// -- that fake is kept as a unit-test fixture only (agent/tests/bridge-v12.test.ts,
// agent/tests/loop-external.test.ts), not as this demo's stand-in for the real binary anymore.
// Set QC_BRIDGE_BIN to override (e.g. a prebuilt binary, or the fake, for a one-off check).
import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { BridgeClient } from "../bridge.js";
import { createExternalModeGateway } from "../broker/external-mode.js";
import { journalSha256RedactingSessions } from "../broker/journal.js";
import { FakeDecider } from "../decider/fake.js";
import { ledgerSha256RedactingApprovalSignatures } from "../ledger.js";
import { runLoop } from "../loop.js";
import { repoRoot } from "../paths.js";
import { MockBroker, type MockScenario } from "./mock-broker.js";
import { resolveRealBridgeBin } from "./real-bridge-bin.js";
import { FIRST_TRADE_DECISION, writeRealBridgeFixtures } from "./real-bridge-fixtures.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** placement order -> broker outcome, matching real-bridge-fixtures.ts's RECORDING (exactly 4
 *  decision points, all buys, in order): a clean fill, a partial fill (the rest cancelled by the
 *  gateway's IOC emulation), a broker rejection, and one that rests unfilled until the IOC cancel
 *  -- "at least one partial fill, one reject, and one cancel" (wave2-spec.md). */
const SCENARIOS: Record<string, MockScenario> = {
  "1": "filled",
  "2": "partially_filled",
  "3": "rejected",
  "4": "accepted",
};

export interface ExternalSimResult {
  ledgerPath: string;
  journalPath: string;
  ledgerSha256: string;
  journalSha256: string;
  counts: { decisions: number; accepted: number; rejected: number; noTrades: number };
}

export interface RunExternalSimOptions {
  /** Directory to write ledger.jsonl/broker-journal.jsonl (and the generated bridge fixtures)
   *  into; defaults to a fresh temp dir per call so two calls in the same process (or two `just`
   *  invocations) never share state unless the caller asks them to. */
  outDir?: string;
}

export async function runExternalSim(opts: RunExternalSimOptions = {}): Promise<ExternalSimResult> {
  const outDir = opts.outDir ?? mkdtempSync(path.join(tmpdir(), "agent-sim-external-"));
  mkdirSync(outDir, { recursive: true });
  const ledgerPath = path.join(outDir, "ledger.jsonl");
  const journalPath = path.join(outDir, "broker-journal.jsonl");

  const bridgeBin = resolveRealBridgeBin();
  const fixtures = writeRealBridgeFixtures(path.join(outDir, "bridge-fixtures"));

  const bridge = new BridgeClient({
    command: bridgeBin,
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
    ],
  });
  bridge.start();

  const broker = new MockBroker({
    scenarios: SCENARIOS,
    // No explicit `position`: the mock derives it from its own recorded fills (order 1 fills in
    // full, order 2 fills half, order 3 is rejected -- see mock-broker.ts), which is what
    // makes it agree with the bridge's OMS at both the startup reconcile (nothing placed yet,
    // position 0) and the one periodic reconcile this demo triggers
    // (reconcileEveryNDecisions below), lands right after client_order_id 3 resolves.
    cash: "10000.00",
  });

  try {
    // "1": the v1 hard-coded instrument label real-bridge-fixtures.ts's RECORDING uses (no
    // `--instrument` flag is passed to the bridge above, so this is what DecisionRequest.instrument
    // actually carries).
    const { gateway, reconcile } = await createExternalModeGateway({
      bridge,
      broker,
      assetClass: "equity",
      instrument: "1",
      journalPath,
      // Every loop order is an approved "ioc", emulated as a day order + a gateway cancel; a short rest
      // keeps the demo fast. Order 4 rests unfilled, so this cancel is the demo's cancel.
      iocCancelAfterMs: 5,
    });

    const counts = await runLoop({
      bridge,
      decider: new FakeDecider({ probThreshold: 0.5 }),
      mode: "fake",
      promptVersion: "v1",
      model: "fake-rule-v1",
      ledgerPath,
      // FIRST_TRADE_DECISION + 2 (not the default 20): real-bridge-fixtures.ts's RECORDING spends
      // its first FIRST_TRADE_DECISION-1 decisions warming up the classifier's feature window
      // (always no_trade, see that file's doc comment) before the 4 trading decisions land; this
      // lands the periodic reconcile exactly once, right after client_order_id 3 resolves and
      // before client_order_id 4 is even placed -- see the mock server construction above for why
      // that specific landing point matters here.
      external: { gateway, assetClass: "equity", reconcile, reconcileEveryNDecisions: FIRST_TRADE_DECISION + 2 },
    });

    return {
      ledgerPath,
      journalPath,
      // Redacted, not the raw file hash: external mode's approval is signed with a fresh
      // Ed25519 key the real bridge generates once per PROCESS and never persists (by design --
      // see ledgerSha256RedactingApprovalSignatures's doc comment), so the raw signature bytes
      // are expected to differ between two independent runs even though every order/decision/
      // execution outcome they wrap is identical. This is what proves that.
      ledgerSha256: ledgerSha256RedactingApprovalSignatures(ledgerPath),
      journalSha256: journalSha256RedactingSessions(journalPath),
      counts,
    };
  } finally {
    await bridge.shutdown();
  }
}

async function main(): Promise<void> {
  const first = await runExternalSim();
  const second = await runExternalSim();

  console.log(`run 1: decisions=${JSON.stringify(first.counts)} ledger(sig-redacted)=${first.ledgerSha256} journal=${first.journalSha256}`);
  console.log(`run 2: decisions=${JSON.stringify(second.counts)} ledger(sig-redacted)=${second.ledgerSha256} journal=${second.journalSha256}`);

  // Keep the latest run's artifacts where other tooling (evals, CI) looks for them, same
  // convention as justfile's agent-sim recipe.
  mkdirSync(path.join(repoRoot(), "out", "agent"), { recursive: true });
  const outLedger = path.join(repoRoot(), "out", "agent", "ledger.jsonl");
  const outJournal = path.join(repoRoot(), "out", "agent", "broker-journal.jsonl");
  const fs = await import("node:fs");
  fs.copyFileSync(second.ledgerPath, outLedger);
  fs.copyFileSync(second.journalPath, outJournal);

  if (first.ledgerSha256 !== second.ledgerSha256) {
    throw new Error(`FAIL: agent-sim-external ledger is not deterministic (${first.ledgerSha256} != ${second.ledgerSha256})`);
  }
  if (first.journalSha256 !== second.journalSha256) {
    throw new Error(`FAIL: agent-sim-external broker journal is not deterministic (${first.journalSha256} != ${second.journalSha256})`);
  }
  console.log("agent-sim-external deterministic across two runs (ledger and broker journal)");
}

// Only run as a CLI when invoked directly (`tsx run-external-sim.ts`), not when imported by a
// test (agent/tests/loop-external.test.ts calls runExternalSim() directly, in-process).
if (path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1] ?? "")) {
  main().catch((err: unknown) => {
    console.error(err instanceof Error ? err.message : err);
    process.exitCode = 1;
  });
}
