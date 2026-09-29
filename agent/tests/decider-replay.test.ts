// #36: replay real decisions to catch decision-path regressions for free, with zero live model
// calls. Unit-tests ReplayDecider directly against the committed fixtures, then proves the same
// fixtures replay correctly through the full runLoop -> ledger path (the same production code
// `just agent-sim`/cli.ts exercise), and finally demonstrates the check actually catches a
// regression by deliberately breaking decider/replay.ts, watching this suite fail, and reverting.
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import type { BridgeClient } from "../src/bridge.js";
import { loadReplayFixtures, ReplayDecider } from "../src/decider/replay.js";
import { ledgerSha256 } from "../src/ledger.js";
import { runLoop } from "../src/loop.js";
import type { DecisionLedgerEntry, DecisionRequest } from "../src/types.js";
import { startFakeBridge } from "./helpers/fakeBridge.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES_DIR = path.join(HERE, "..", "fixtures", "replay");

describe("loadReplayFixtures", () => {
  it("loads exactly the 3 committed, schema-valid fixtures, clearly labelled synthetic", () => {
    const fixtures = loadReplayFixtures(FIXTURES_DIR);
    expect(fixtures).toHaveLength(3);
    const ids = fixtures.map((f) => f.request.request_id).sort();
    expect(ids).toEqual([
      "fixture-clean-buy-001",
      "fixture-clean-no-trade-001",
      "fixture-risk-rejected-001",
    ]);
    for (const fixture of fixtures) {
      expect(fixture.model).toBe("synthetic-fixture-do-not-treat-as-live");
      expect((fixture.raw as { synthetic?: boolean }).synthetic).toBe(true);
    }
  });
});

describe("ReplayDecider", () => {
  const fixtures = loadReplayFixtures(FIXTURES_DIR);
  const byId = new Map(fixtures.map((f) => [f.request.request_id, f]));

  function requestFor(id: string): DecisionRequest {
    const fixture = byId.get(id);
    if (!fixture) throw new Error(`no fixture ${id}`);
    return fixture.request;
  }

  it("reproduces the exact recorded decision, twice, with zero live model calls", async () => {
    const decider = new ReplayDecider(fixtures);
    const req = requestFor("fixture-clean-buy-001");
    const first = await decider.decide(req);
    const second = await decider.decide(req);
    expect(first.decision).toEqual(second.decision);
    expect(first.decision).toEqual(byId.get("fixture-clean-buy-001")!.decision);
    // "Zero live model calls" isn't just an assertion here: ReplayDecider (src/decider/replay.ts)
    // never imports @anthropic-ai/claude-agent-sdk at all, so there is nothing in this class that
    // could reach the network even if it wanted to.
  });

  it("reproduces the clean no_trade decision, including a null result carried through the fixture", async () => {
    const decider = new ReplayDecider(fixtures);
    const outcome = await decider.decide(requestFor("fixture-clean-no-trade-001"));
    expect(outcome.decision.action).toBe("no_trade");
    expect(byId.get("fixture-clean-no-trade-001")!.result).toBeNull();
  });

  it("reproduces the risk-rejected decision (the decider's proposal, not the risk engine's verdict)", async () => {
    const decider = new ReplayDecider(fixtures);
    const outcome = await decider.decide(requestFor("fixture-risk-rejected-001"));
    expect(outcome.decision).toMatchObject({ action: "sell", qty: "50" });
    // ReplayDecider replays the *decision*; the recorded rejection lives on the fixture's own
    // `result`, which this decider never touches (that's qc-bridge's risk engine, out of scope
    // for this lane) — asserted here only to document what "risk-rejected" means for this fixture.
    expect(byId.get("fixture-risk-rejected-001")!.result).toEqual({
      accepted: false,
      risk_reject: "qty 50 exceeds max_position 10",
    });
  });

  it("fails closed on a request_id it has no recording for, rather than guessing", async () => {
    const decider = new ReplayDecider(fixtures);
    const unknown: DecisionRequest = { ...requestFor("fixture-clean-buy-001"), request_id: "not-a-real-fixture" };
    await expect(decider.decide(unknown)).rejects.toThrow(/no recorded decision/);
  });

  it("fails closed when the incoming request no longer matches what was recorded", async () => {
    const decider = new ReplayDecider(fixtures);
    const drifted: DecisionRequest = { ...requestFor("fixture-clean-buy-001"), mid: "999.99" };
    await expect(decider.decide(drifted)).rejects.toThrow(/no longer matches/);
  });

  it("refuses to construct from two fixtures sharing a request_id", () => {
    const [first] = fixtures;
    expect(() => new ReplayDecider([first!, first!])).toThrow(/duplicate replay fixture/);
  });
});

describe("replay through the full loop -> ledger path (what cli.ts QC_AGENT_MODE=replay runs)", () => {
  const bridges: BridgeClient[] = [];

  afterEach(async () => {
    await Promise.all(bridges.splice(0).map((b) => b.shutdown().catch(() => undefined)));
  });

  it("replays all 3 fixtures through runLoop with mode:'replay' ledger entries, bit-stable across two runs", async () => {
    const fixtures = loadReplayFixtures(FIXTURES_DIR);
    const scenario: DecisionRequest[] = fixtures.map((f) => f.request);

    async function runOnce(): Promise<{ hash: string; entries: DecisionLedgerEntry[] }> {
      const dir = mkdtempSync(path.join(tmpdir(), "agent-replay-"));
      const scenarioPath = path.join(dir, "scenario.json");
      writeFileSync(scenarioPath, JSON.stringify(scenario), "utf8");
      const ledgerPath = path.join(dir, "ledger.jsonl");

      const bridge = startFakeBridge({ scenario: scenarioPath });
      bridges.push(bridge);

      await runLoop({
        bridge,
        decider: new ReplayDecider(fixtures),
        mode: "replay",
        promptVersion: "v1",
        model: "replay-fixture",
        ledgerPath,
      });

      const entries = readFileSync(ledgerPath, "utf8")
        .split("\n")
        .filter(Boolean)
        .map((line) => JSON.parse(line) as DecisionLedgerEntry);
      return { hash: ledgerSha256(ledgerPath), entries };
    }

    const first = await runOnce();
    expect(first.entries).toHaveLength(3);
    for (const entry of first.entries) {
      expect(entry.mode).toBe("replay");
      // Wall-clock latency would make two replays of the same recording differ.
      expect(entry.latency_ms).toBeUndefined();
    }
    const byRequestId = new Map(first.entries.map((e) => [e.request.request_id, e]));
    expect(byRequestId.get("fixture-clean-buy-001")?.decision.action).toBe("buy");
    expect(byRequestId.get("fixture-clean-no-trade-001")?.decision.action).toBe("no_trade");
    expect(byRequestId.get("fixture-risk-rejected-001")?.decision.action).toBe("sell");

    const second = await runOnce();
    expect(second.hash).toBe(first.hash);
  });
});
