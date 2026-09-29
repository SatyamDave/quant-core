// Issue #45 (TS side): reconcileWithBroker() pulls what the mock broker reports and hands it to
// a BridgeReconciler double, proving a deliberately-injected mismatch (our "OMS" via the bridge
// double disagreeing with the mock's own position/cash) is flagged rather than resolved by trusting either side.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { ReconciliationHalted, reconcileWithBroker } from "../../src/broker/reconcile.js";
import { repoRoot } from "../../src/paths.js";
import { MockBroker } from "../../src/testkit/mock-broker.js";
import type { VenueSnapshot } from "../../src/types.js";

describe("reconcileWithBroker", () => {
  const mockBroker = async (position: string, cash: string | null) => new MockBroker({ position, cash });

  it("passes the broker's reported orders/position/cash straight through to the bridge, unmodified", async () => {
    const broker = await mockBroker("3", "12345.67");
    let seen: VenueSnapshot | undefined;
    const bridge = {
      reconcile: async (venue: VenueSnapshot) => {
        seen = venue;
        return { ok: true, reconcile: { ok: true, discrepancies: [] } };
      },
    };

    await reconcileWithBroker({ broker, bridge, assetClass: "equity", instrument: "SPY" });

    expect(seen).toBeDefined();
    expect(seen!.position).toBe("3");
    expect(seen!.cash).toBe("12345.67");
    expect(seen!.orders).toEqual([]);
  });

  it("throws ReconciliationHalted when the bridge reports a mismatch -- never silently continues", async () => {
    // Wave-2 review fix: the real bridge answers the RPC envelope's ok:true for a successful
    // comparison regardless of outcome; the clean/mismatch verdict is nested under "reconcile"
    // (schemas/decision/v1/README.md's v1.2 section). An earlier version of this test (and of
    // reconcileWithBroker itself) used the envelope's own ok for this, which the real bridge
    // never sets to false for a mere mismatch -- only for a malformed request.
    const broker = await mockBroker("3", "12345.67");
    const bridge = {
      reconcile: async () => ({
        ok: true,
        reconcile: { ok: false, discrepancies: ["position mismatch: our OMS has 5, broker reports 3"] },
      }),
    };

    await expect(reconcileWithBroker({ broker, bridge, assetClass: "equity", instrument: "SPY" })).rejects.toThrow(
      ReconciliationHalted,
    );
    await expect(reconcileWithBroker({ broker, bridge, assetClass: "equity", instrument: "SPY" })).rejects.toThrow(
      /position mismatch/,
    );
  });

  it("throws ReconciliationHalted when the RPC envelope itself reports failure (a protocol error, not a mismatch)", async () => {
    const broker = await mockBroker("3", "12345.67");
    const bridge = {
      reconcile: async () => ({ ok: false, error: { code: "invalid_fields", message: '"venue" is required' } }),
    };

    await expect(reconcileWithBroker({ broker, bridge, assetClass: "equity", instrument: "SPY" })).rejects.toThrow(
      ReconciliationHalted,
    );
  });

  it("clean compare (reconcile.ok:true) resolves without throwing", async () => {
    const broker = await mockBroker("0", "10000.00");
    const bridge = { reconcile: async () => ({ ok: true, reconcile: { ok: true, discrepancies: [] } }) };

    await expect(reconcileWithBroker({ broker, bridge, assetClass: "equity", instrument: "SPY" })).resolves.toBeUndefined();
  });

  it("a response with no nested verdict is not clean: throws", async () => {
    const broker = await mockBroker("0", "10000.00");
    const bridge = { reconcile: async () => ({ ok: true }) };

    await expect(reconcileWithBroker({ broker, bridge, assetClass: "equity", instrument: "SPY" })).rejects.toThrow(
      /without a verdict/,
    );
  });
});

/** Runs scripts/ops/preflight.py's own check_reconcile_recent on `statusPath`, so these tests
 *  check the writer against preflight's contract rather than a copy of it. `ageMin` shifts
 *  preflight's "now" past the recorded ts_ns. */
function preflightGate(statusPath: string, ageMin: number): { passed: boolean; detail: string } {
  const script = `
import json, sys
from pathlib import Path
sys.path.insert(0, "scripts/ops")
import preflight
ts_ns = json.loads(Path(sys.argv[1]).read_text())["ts_ns"]
now_ns = ts_ns + int(float(sys.argv[2]) * preflight.MINUTE_NS)
r = preflight.check_reconcile_recent(Path("."), Path(sys.argv[1]), now_ns, preflight.DEFAULT_MAX_RECONCILE_AGE_MIN)
print(json.dumps({"passed": r.ok, "detail": r.detail}))
`;
  const res = spawnSync("python3", ["-c", script, statusPath, String(ageMin)], { cwd: repoRoot(), encoding: "utf8" });
  if (res.status !== 0) throw new Error(`preflight gate harness failed: ${res.stderr}`);
  return JSON.parse(res.stdout);
}

describe("reconcile status file (preflight's reconcile_recent gate)", () => {
  const brokerFor = async (position: string) => new MockBroker({ position, cash: "10000.00" });

  const tmpStatus = () => path.join(mkdtempSync(path.join(os.tmpdir(), "reconcile-status-")), "state", "status.json");
  const clean = { reconcile: async () => ({ ok: true, reconcile: { ok: true, discrepancies: [] } }) };
  const mismatch = {
    reconcile: async () => ({ ok: true, reconcile: { ok: false, discrepancies: ["position mismatch: 5 vs 3"] } }),
  };

  it("the default path matches preflight's DEFAULT_RECONCILE_STATUS", async () => {
    const { reconcileStatusPath } = await import("../../src/paths.js");
    const res = spawnSync(
      "python3",
      ["-c", 'import sys; sys.path.insert(0, "scripts/ops"); import preflight; print(preflight.DEFAULT_RECONCILE_STATUS)'],
      { cwd: repoRoot(), encoding: "utf8" },
    );
    expect(res.status).toBe(0);
    expect(path.relative(repoRoot(), reconcileStatusPath())).toBe(res.stdout.trim());
  });

  it("a clean reconcile writes a status preflight passes", async () => {
    const statusPath = tmpStatus();
    await reconcileWithBroker({ broker: await brokerFor("0"), bridge: clean, assetClass: "equity", instrument: "SPY", statusPath });

    expect(preflightGate(statusPath, 1).passed).toBe(true);
  });

  it("a stale clean reconcile fails preflight", async () => {
    const statusPath = tmpStatus();
    await reconcileWithBroker({ broker: await brokerFor("0"), bridge: clean, assetClass: "equity", instrument: "SPY", statusPath });

    expect(preflightGate(statusPath, 61).passed).toBe(false);
  });

  it("a mismatch overwrites an earlier clean status and fails preflight", async () => {
    const statusPath = tmpStatus();
    const broker = await brokerFor("3");
    await reconcileWithBroker({ broker, bridge: clean, assetClass: "equity", instrument: "SPY", statusPath });
    await expect(
      reconcileWithBroker({ broker, bridge: mismatch, assetClass: "equity", instrument: "SPY", statusPath }),
    ).rejects.toThrow(ReconciliationHalted);

    const gate = preflightGate(statusPath, 1);
    expect(gate.passed).toBe(false);
    expect(gate.detail).toMatch(/position mismatch/);
  });

  it("a reconcile that could not reach the bridge overwrites an earlier clean status and fails preflight", async () => {
    const statusPath = tmpStatus();
    const broker = await brokerFor("0");
    await reconcileWithBroker({ broker, bridge: clean, assetClass: "equity", instrument: "SPY", statusPath });
    const broken = {
      reconcile: async () => {
        throw new Error("bridge pipe closed");
      },
    };
    await expect(
      reconcileWithBroker({ broker, bridge: broken, assetClass: "equity", instrument: "SPY", statusPath }),
    ).rejects.toThrow(/pipe closed/);

    expect(JSON.parse(readFileSync(statusPath, "utf8")).ok).toBe(false);
    expect(preflightGate(statusPath, 1).passed).toBe(false);
  });
});
