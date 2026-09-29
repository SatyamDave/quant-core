import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  type ControlBridge,
  engageKillSwitch,
  getRules,
  getStatus,
  killSwitchState,
  listDecisions,
  listOrders,
  proposeRuleChange,
  submitOrderIntent,
} from "../src/control/index.js";
import type { BridgeResponse, OrderIntent } from "../src/types.js";

const LIMITS = `# header comment
max_notional = "25"          # USD per order
max_order_rate_per_sec = 5
wash_trade_window_ms = 2000
`;

let dir: string;
let opts: { limitsFile: string; killFile: string; ledgerFile: string };

beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "qc-control-"));
  opts = { limitsFile: path.join(dir, "limits.toml"), killFile: path.join(dir, "KILL"), ledgerFile: path.join(dir, "ledger.jsonl") };
  writeFileSync(opts.limitsFile, LIMITS);
});

function bridgeMock(venue: "sim" | "external" = "sim") {
  const submitOrderIntent = vi.fn(
    async (): Promise<BridgeResponse> => ({ v: 1, id: "x", ok: true, result: { accepted: true, client_order_id: 7 } }),
  );
  const bridge: ControlBridge = {
    hello: async () => ({ v: 1, id: "h", ok: true, hello: { protocol: "1.2", venue_mode: venue, approval_public_key: "k" } }),
    status: async () => ({ v: 1, id: "s", ok: true, status: { position: "0", halted: null } }),
    submitOrderIntent,
  };
  return { bridge, submitOrderIntent };
}

const intent: OrderIntent = {
  request_id: "r1", instrument: "SPY", side: "buy", qty: "1", limit_price: "10", time_in_force: "ioc", reason: "test",
};

describe("rules", () => {
  it("reads the limits file", () => {
    expect(getRules(opts)).toEqual({ max_notional: "25", max_order_rate_per_sec: "5", wash_trade_window_ms: "2000" });
  });

  it("applies a tightening change and keeps comments", () => {
    expect(proposeRuleChange({ key: "max_notional", value: "10" }, opts)).toEqual({ key: "max_notional", from: "25", to: "10" });
    proposeRuleChange({ key: "wash_trade_window_ms", value: "5000" }, opts); // wider window is tighter
    const text = readFileSync(opts.limitsFile, "utf8");
    expect(text).toContain('max_notional = "10"          # USD per order');
    expect(getRules(opts).wash_trade_window_ms).toBe("5000");
  });

  it("rejects a loosening change and leaves the file untouched", () => {
    expect(() => proposeRuleChange({ key: "max_notional", value: "100" }, opts)).toThrow(/tighten-only.*human/);
    expect(() => proposeRuleChange({ key: "wash_trade_window_ms", value: "100" }, opts)).toThrow(/loosens/);
    expect(() => proposeRuleChange({ key: "not_a_limit", value: "1" }, opts)).toThrow(/unknown limit/);
    expect(readFileSync(opts.limitsFile, "utf8")).toBe(LIMITS);
  });
});

describe("kill switch", () => {
  it("toggles from off to engaged via the kill file", () => {
    expect(killSwitchState(opts).engaged).toBe(false);
    engageKillSwitch("test", opts);
    expect(killSwitchState(opts)).toEqual({ engaged: true, file: opts.killFile });
  });
});

describe("order intents", () => {
  it("routes through the bridge's submit_order_intent", async () => {
    const { bridge, submitOrderIntent: spy } = bridgeMock();
    expect(await submitOrderIntent(intent, { ...opts, bridge })).toEqual({ accepted: true, client_order_id: 7 });
    expect(spy).toHaveBeenCalledWith(intent);
  });

  it("refuses an external-venue bridge by default and never calls it once killed", async () => {
    const ext = bridgeMock("external");
    await expect(submitOrderIntent(intent, { ...opts, bridge: ext.bridge })).rejects.toThrow(/external/);
    expect(ext.submitOrderIntent).not.toHaveBeenCalled();

    const sim = bridgeMock();
    engageKillSwitch("test", opts);
    expect(await submitOrderIntent(intent, { ...opts, bridge: sim.bridge })).toEqual({ accepted: false, halted: "kill_switch" });
    expect(sim.submitOrderIntent).not.toHaveBeenCalled();
  });
});

describe("status and ledger", () => {
  it("summarizes today's usage from the ledger", async () => {
    const ts = Date.parse("2026-09-29T15:00:00Z") * 1e6;
    const entry = (accepted: boolean) =>
      JSON.stringify({
        request: { ts_ns: ts },
        decision: { request_id: "r", action: "buy", qty: "2", limit_price: "10.5", rationale: "" },
        result: { accepted },
      });
    writeFileSync(opts.ledgerFile, `${entry(true)}\n${entry(false)}\n`);
    const { bridge } = bridgeMock();
    const s = await getStatus({ ...opts, bridge, now: () => new Date("2026-09-29T20:00:00Z") });
    expect(s.mode).toBe("sim");
    expect(s.today).toEqual({ date: "2026-09-29", decisions: 2, ordersAccepted: 1, notional: "21" });
    expect(s.limits.max_notional).toBe("25");
    expect(listDecisions(10, opts)).toHaveLength(2);
    expect(listOrders(10, opts)).toHaveLength(1);
  });
});
