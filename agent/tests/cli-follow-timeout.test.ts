// scripts/shadow/run.py runs qc-bridge --follow, which holds a next_decision_request answer until
// the growing recording yields a decision. This drives the real entry point (src/cli.ts) against
// fake-bridge.ts holding its first answer longer than BridgeClient's 5s default, and checks that
// QC_BRIDGE_DECISION_TIMEOUT_MS=0 waits it out while a bridge that dies still fails the agent.
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { AGENT_ROOT, FAKE_BRIDGE_SCRIPT, SCENARIO_FIXTURE, TSX_BIN } from "./helpers/fakeBridge.js";

const QUIET_MS = 6_000; // longer than bridge.ts's DEFAULT_TIMEOUT_MS (5s)

function runCli(env: Record<string, string>): Promise<{ status: number | null; stderr: string }> {
  const ledgerDir = mkdtempSync(path.join(tmpdir(), "cli-follow-"));
  const child = spawn(TSX_BIN, [path.join(AGENT_ROOT, "src", "cli.ts")], {
    cwd: AGENT_ROOT,
    env: {
      ...process.env,
      QC_AGENT_MODE: "fake",
      QC_BRIDGE_BIN: TSX_BIN,
      QC_BRIDGE_ARGS: `${FAKE_BRIDGE_SCRIPT} --scenario ${SCENARIO_FIXTURE}`,
      QC_AGENT_LEDGER_PATH: path.join(ledgerDir, "ledger.jsonl"),
      ...env,
    },
  });
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += d));
  return new Promise((resolve) => child.on("close", (status) => resolve({ status, stderr })));
}

describe("the agent waits through a quiet follow-mode bridge but not a dead one", () => {
  it("default timeout: a quiet bridge kills the agent; 0: it waits and exits zero", async () => {
    const quiet = { QC_FAKE_BRIDGE_MODE: "quiet_next_decision_request", QC_FAKE_BRIDGE_QUIET_MS: String(QUIET_MS) };
    const [bounded, unbounded] = await Promise.all([
      runCli(quiet),
      runCli({ ...quiet, QC_BRIDGE_DECISION_TIMEOUT_MS: "0" }),
    ]);
    expect(bounded.status).toBe(1);
    expect(bounded.stderr).toMatch(/next_decision_request .* timed out after 5000ms/);
    expect(unbounded.stderr).toBe("");
    expect(unbounded.status).toBe(0);
  }, 30_000);

  it("a bridge that dies while the agent waits with no timeout still exits the agent non-zero", async () => {
    const result = await runCli({
      QC_FAKE_BRIDGE_MODE: "quiet_then_crash",
      QC_FAKE_BRIDGE_QUIET_MS: "500",
      QC_BRIDGE_DECISION_TIMEOUT_MS: "0",
    });
    expect(result.status).toBe(1);
    expect(result.stderr).toMatch(/bridge process exited \(code=1/);
  }, 15_000);

  it("refuses a malformed QC_BRIDGE_DECISION_TIMEOUT_MS instead of guessing", async () => {
    const result = await runCli({ QC_BRIDGE_DECISION_TIMEOUT_MS: "-1" });
    expect(result.status).toBe(1);
    expect(result.stderr).toMatch(/QC_BRIDGE_DECISION_TIMEOUT_MS must be a non-negative integer/);
  }, 15_000);
});
