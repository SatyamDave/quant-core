// wave2-spec.md's `just agent-sim-external`: agent (fake) -> the real qc-bridge --venue external
// -> gateway -> mock broker -> report_execution, run twice with identical ledger (approval
// signatures redacted -- see ledgerSha256RedactingApprovalSignatures's doc comment: the real
// bridge signs with a fresh per-process key on purpose, so the raw bytes can't match, only
// everything they wrap) and broker-journal hashes, exercising at least one partial fill, one
// reject, and one cancel. This is the same harness the justfile recipe uses
// (agent/src/testkit/run-external-sim.ts), called in-process here so this suite (agent-test, no
// shell-out) proves the same property. Spawns the real qc-bridge release binary (built on demand
// by real-bridge-bin.ts), so this suite needs `cargo` on PATH, same as `just agent-sim`'s own
// real-binary tests elsewhere in this repo.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { FIRST_TRADE_DECISION } from "../src/testkit/real-bridge-fixtures.js";
import { runExternalSim } from "../src/testkit/run-external-sim.js";

describe("agent-sim-external: the full external-mode pipeline, twice", () => {
  it("produces identical ledger and broker-journal hashes across two independent runs", async () => {
    const first = await runExternalSim();
    const second = await runExternalSim();

    expect(first.ledgerSha256).toMatch(/^[0-9a-f]{64}$/);
    expect(first.journalSha256).toMatch(/^[0-9a-f]{64}$/);
    expect(second.ledgerSha256).toBe(first.ledgerSha256);
    expect(second.journalSha256).toBe(first.journalSha256);
  }, 20_000);

  it("exercises a partial fill, a reject, and a cancel", async () => {
    const result = await runExternalSim();
    const ledgerLines = readFileSync(result.ledgerPath, "utf8").trim().split("\n").map((l) => JSON.parse(l) as Record<string, unknown>);
    const journalLines = readFileSync(result.journalPath, "utf8").trim().split("\n").map((l) => JSON.parse(l) as Record<string, unknown>);

    const results = ledgerLines.map((e) => e.result as Record<string, unknown> | null).filter((r): r is Record<string, unknown> => r !== null);
    expect(results.some((r) => r.client_order_id === 1 && r.accepted === true)).toBe(true); // filled, exercised via journal below
    expect(results.some((r) => r.client_order_id === 3 && r.accepted === true)).toBe(true); // accepted at the bridge; rejected by the broker

    const resolvedByOrder = new Map<number, { status: string; filled_qty?: string }>();
    for (const line of journalLines) {
      if (line.phase !== "resolved") continue;
      resolvedByOrder.set(line.client_order_id as number, line.result as { status: string; filled_qty?: string });
    }
    expect(resolvedByOrder.get(1)?.status).toBe("filled");
    // Emulated IOC: half filled, the rest cancelled by the gateway after its rest window.
    expect(resolvedByOrder.get(2)?.status).toBe("canceled");
    expect(resolvedByOrder.get(2)?.filled_qty).not.toBe("0");
    expect(resolvedByOrder.get(3)?.status).toBe("rejected");
    expect(resolvedByOrder.get(4)).toMatchObject({ status: "canceled", filled_qty: "0" }); // rested, then the IOC cancel

    // real-bridge-fixtures.ts's RECORDING spends its first FIRST_TRADE_DECISION-1 decisions
    // warming up the classifier's feature window (always no_trade) before the 4 trading
    // decisions that exercise fill/partial-fill/reject/cancel above.
    expect(result.counts.decisions).toBe(FIRST_TRADE_DECISION + 3);
    expect(result.counts.noTrades).toBe(FIRST_TRADE_DECISION - 1);
    expect(result.counts.accepted).toBe(4); // all 4 accepted at the bridge; order 3's rejection is broker-side
  }, 20_000);
});
