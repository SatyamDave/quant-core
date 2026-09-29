// wave2-spec.md gap: "no test that the agent service exits when its bridge dies." This runs the
// real entry point (src/cli.ts) as an actual child process -- not just a unit test of
// BridgeClient's internal error propagation -- against fake-bridge-v12.ts in a mode that kills
// itself mid-run, and asserts the AGENT SERVICE's own process exit code is non-zero. That is the
// thing an operator (or a supervisor like ops/live/supervisor.py) actually observes.
import { spawnSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { AGENT_ROOT, TSX_BIN } from "./helpers/fakeBridge.js";

describe("the agent service exits non-zero when its qc-bridge child dies", () => {
  it("cli.ts's own process exit code is non-zero after the bridge process exits mid-loop", () => {
    const ledgerDir = mkdtempSync(path.join(tmpdir(), "cli-exit-"));
    const bridgeScript = path.join(AGENT_ROOT, "src", "testkit", "fake-bridge-v12.ts");
    const scenario = path.join(AGENT_ROOT, "tests", "fixtures", "scenario.json");

    const result = spawnSync(TSX_BIN, [path.join(AGENT_ROOT, "src", "cli.ts")], {
      cwd: AGENT_ROOT,
      env: {
        ...process.env,
        QC_AGENT_MODE: "fake",
        QC_BRIDGE_BIN: TSX_BIN,
        QC_BRIDGE_ARGS: `${bridgeScript} --venue sim --scenario ${scenario}`,
        QC_FAKE_BRIDGE_V12_MODE: "crash_after_first_decision",
        QC_AGENT_LEDGER_PATH: path.join(ledgerDir, "ledger.jsonl"),
      },
      encoding: "utf8",
      timeout: 15_000,
    });

    expect(result.status).not.toBe(0);
    expect(result.status).not.toBeNull(); // not killed by a signal/timeout either
  });
});
