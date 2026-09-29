import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { appendLedgerEntry, ledgerSha256 } from "../src/ledger.js";
import type { DecisionLedgerEntry } from "../src/types.js";

function tempLedgerPath(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "agent-ledger-unit-"));
  return path.join(dir, "ledger.jsonl");
}

const VALID_ENTRY: DecisionLedgerEntry = {
  request: {
    request_id: "req-1",
    ts_ns: 1,
    instrument: "SIM-BTC",
    best_bid: "100.0",
    best_ask: "100.1",
    mid: "100.05",
    spread_ticks: 1,
    features: {},
    signal: null,
    position: "0",
    limits: {
      max_position: "10",
      max_notional: "100000",
      max_order_rate_per_sec: 5,
      remaining_daily_loss: "1000",
    },
    allowed_actions: ["buy", "sell", "no_trade"],
  },
  decision: { request_id: "req-1", action: "no_trade", rationale: "no signal" },
  result: null,
  mode: "fake",
  prompt_version: "v1",
  model: "fake-rule-v1",
};

describe("ledger", () => {
  it("appends one JSON line per entry and never truncates", () => {
    const filePath = tempLedgerPath();
    appendLedgerEntry(VALID_ENTRY, filePath);
    appendLedgerEntry({ ...VALID_ENTRY, decision: { ...VALID_ENTRY.decision, request_id: "req-2" } }, filePath);

    const lines = readFileSync(filePath, "utf8").trim().split("\n");
    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[0]!).decision.request_id).toBe("req-1");
    expect(JSON.parse(lines[1]!).decision.request_id).toBe("req-2");
  });

  it("refuses to append an entry that fails schema validation, and leaves the file untouched", () => {
    const filePath = tempLedgerPath();
    appendLedgerEntry(VALID_ENTRY, filePath);
    const before = readFileSync(filePath, "utf8");

    const invalid = { ...VALID_ENTRY, mode: "not-a-real-mode" } as unknown as DecisionLedgerEntry;
    expect(() => appendLedgerEntry(invalid, filePath)).toThrow(/schema validation failed/);

    expect(readFileSync(filePath, "utf8")).toBe(before);
  });

  it("sha256 is stable for identical content and changes when content changes", () => {
    const a = tempLedgerPath();
    const b = tempLedgerPath();
    appendLedgerEntry(VALID_ENTRY, a);
    appendLedgerEntry(VALID_ENTRY, b);
    expect(ledgerSha256(a)).toBe(ledgerSha256(b));

    appendLedgerEntry({ ...VALID_ENTRY, decision: { ...VALID_ENTRY.decision, request_id: "req-2" } }, b);
    expect(ledgerSha256(a)).not.toBe(ledgerSha256(b));
  });

  it("sha256 of a ledger that was never written is the empty-input hash", () => {
    const filePath = tempLedgerPath();
    expect(ledgerSha256(filePath)).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  });
});
