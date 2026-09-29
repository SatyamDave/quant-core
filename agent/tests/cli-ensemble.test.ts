// Issue #54: proves the actual wiring in src/cli.ts (QC_ENSEMBLE_SIZE/QC_ENSEMBLE_AGGREGATION/
// QC_ENSEMBLE_FAKE_PROB_THRESHOLDS -> an EnsembleDecider whose per-member votes land in the
// ledger) end to end, as a real child process -- the same style as cli-exit.test.ts -- rather
// than only unit-testing parseEnsembleEnv/aggregate in isolation (decider-ensemble.test.ts
// already does that). No network, no API key: fake mode only.
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { describe, expect, it } from "vitest";
import { AGENT_ROOT, FAKE_BRIDGE_SCRIPT, SCENARIO_FIXTURE, TSX_BIN } from "./helpers/fakeBridge.js";

interface LedgerEntry {
  request: { request_id: string };
  decision: { action: string };
  model: string;
  raw?: { aggregation: string; members: { label: string; decision: { action: string } }[] } | null;
}

function runCli(env: Record<string, string>): { status: number | null; ledgerPath: string; entries: LedgerEntry[] } {
  const ledgerDir = mkdtempSync(path.join(tmpdir(), "cli-ensemble-"));
  const ledgerPath = path.join(ledgerDir, "ledger.jsonl");
  const result = spawnSync(TSX_BIN, [path.join(AGENT_ROOT, "src", "cli.ts")], {
    cwd: AGENT_ROOT,
    env: {
      ...process.env,
      QC_AGENT_MODE: "fake",
      QC_BRIDGE_BIN: TSX_BIN,
      QC_BRIDGE_ARGS: `${FAKE_BRIDGE_SCRIPT} --scenario ${SCENARIO_FIXTURE}`,
      QC_AGENT_LEDGER_PATH: ledgerPath,
      ...env,
    },
    encoding: "utf8",
    timeout: 15_000,
  });
  const entries: LedgerEntry[] =
    result.status === 0
      ? readFileSync(ledgerPath, "utf8")
          .trim()
          .split("\n")
          .filter(Boolean)
          .map((line) => JSON.parse(line) as LedgerEntry)
      : [];
  if (result.status !== 0) {
    // Surface the failure in the assertion output rather than a bare "expected 0, got 1".
    console.error(result.stdout, result.stderr);
  }
  return { status: result.status, ledgerPath, entries };
}

describe("cli.ts ensemble wiring (fake mode, no network)", () => {
  it("with QC_ENSEMBLE_SIZE unset, behaves exactly as a single decider (no raw.aggregation)", () => {
    const { status, entries } = runCli({});
    expect(status).toBe(0);
    expect(entries).toHaveLength(2);
    // FakeDecider reports no `raw` at all; the ledger writes DecisionOutcome.raw ?? null.
    for (const entry of entries) expect(entry.raw).toBeNull();
  });

  it("wires QC_ENSEMBLE_SIZE/AGGREGATION/FAKE_PROB_THRESHOLDS into a real majority vote", () => {
    const { status, entries } = runCli({
      QC_ENSEMBLE_SIZE: "3",
      QC_ENSEMBLE_AGGREGATION: "majority",
      QC_ENSEMBLE_FAKE_PROB_THRESHOLDS: "0.5,0.6,0.8",
    });
    expect(status).toBe(0);
    expect(entries).toHaveLength(2);

    // req-1: signal up at prob 0.7. Two members (thresholds 0.5, 0.6) clear it and propose buy;
    // the third (threshold 0.8) does not and proposes no_trade -- a real 2-vs-1 disagreement.
    const first = entries[0]!;
    expect(first.request.request_id).toBe("req-1");
    expect(first.raw?.aggregation).toBe("majority");
    expect(first.raw?.members).toHaveLength(3);
    expect(first.raw?.members.map((m) => m.decision.action)).toEqual(["buy", "buy", "no_trade"]);
    expect(first.decision.action).toBe("buy"); // 2/3 majority
    expect(first.model).toBe("ensemble(majority,n=3,fake-rule-v1)");

    // req-2: signal flat -- every member's rule declines regardless of threshold (correlated
    // agreement, not a real test of the aggregation rule, but exactly what "unanimous no_trade"
    // should look like).
    const second = entries[1]!;
    expect(second.raw?.members.every((m) => m.decision.action === "no_trade")).toBe(true);
    expect(second.decision.action).toBe("no_trade");
  });

  it("refuses to start when QC_ENSEMBLE_SIZE is set without QC_ENSEMBLE_AGGREGATION", () => {
    const { status, entries } = runCli({ QC_ENSEMBLE_SIZE: "3" });
    expect(status).not.toBe(0);
    expect(entries).toHaveLength(0);
  });
});
