// PR #107 review findings, one test each: a bridge restart reuses client_order_id 1, an approval
// that expires during the preview, and a fill seen before an unconfirmed IOC cancel. The broker is
// a scripted in-process fake BrokerAdapter (synthetic responses only).
import { generateKeyPairSync, sign, type KeyObject } from "node:crypto";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ed25519PublicKeyFromRaw } from "../../src/broker/crypto.js";
import { BrokerGateway, GatewayRejection } from "../../src/broker/gateway.js";
import type { BrokerAdapter } from "../../src/broker/adapter.js";
import type { Approval, ApprovedOrder, BridgeReporter, ReportExecutionOp } from "../../src/broker/types.js";

const NOW_NS = 1_767_571_200_000_000_000;

type Method = keyof BrokerAdapter;
type Handler = (args: Record<string, unknown>) => unknown;
interface Call {
  name: Method;
  args: Record<string, unknown>;
  timeout?: number;
}

/** A venue order record (or preview) in the adapter's normalized vocabulary. */
const venue = (fields: Record<string, unknown>) => ({ raw: {}, ...fields });

// Positional argument names per method, so a handler reads `a.refId`, `a.order` etc.
const ARG_NAMES: Record<Method, string[]> = {
  verifyAccount: [],
  previewOrder: ["assetClass", "order"],
  placeOrder: ["assetClass", "order", "refId"],
  cancelOrder: ["assetClass", "venueOrderId"],
  getOrderById: ["assetClass", "venueOrderId"],
  findOrderByRefId: ["assetClass", "refId", "symbol", "createdAtGte"],
  listOrders: ["assetClass"],
  getAccountState: ["assetClass", "instrument"],
  getQuote: ["assetClass", "instrument"],
  getBook: ["assetClass", "instrument"],
};

function fakeBroker(handlers: Partial<Record<Method, Handler>>, calls: Call[] = []): BrokerAdapter {
  const broker = {} as Record<Method, (...a: unknown[]) => Promise<unknown>>;
  for (const [name, argNames] of Object.entries(ARG_NAMES) as Array<[Method, string[]]>) {
    broker[name] = async (...a: unknown[]) => {
      const args = Object.fromEntries(argNames.map((n, i) => [n, a[i]]));
      calls.push({ name, args, timeout: (a[argNames.length] as { timeoutMs?: number } | undefined)?.timeoutMs });
      const handler = handlers[name];
      if (!handler) throw new Error(`fake broker: no handler for ${name}`);
      return handler(args);
    };
  }
  return broker as unknown as BrokerAdapter;
}
const limitOf = (a: Record<string, unknown>) => (a.order as ApprovedOrder).limit_price;

class Reporter implements BridgeReporter {
  readonly calls: ReportExecutionOp[] = [];
  async reportExecution(op: ReportExecutionOp) {
    this.calls.push(op);
    return { ok: true };
  }
  events() {
    return this.calls.map((c) => [c.execution.event, c.execution.qty, c.execution.price]);
  }
}

/** One simulated qc-bridge process: its own fresh Ed25519 key, as the real bridge mints per start. */
function bridgeSession() {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const key: KeyObject = ed25519PublicKeyFromRaw((publicKey.export({ type: "spki", format: "der" }) as Buffer).subarray(-32));
  const approve = (o: ApprovedOrder, expiresTsNs = NOW_NS + 5_000_000_000): Approval => {
    const payload = JSON.stringify({
      client_order_id: o.client_order_id,
      expires_ts_ns: expiresTsNs,
      instrument: o.instrument,
      limit_price: o.limit_price,
      qty: o.qty,
      request_id: o.request_id,
      side: o.side,
      time_in_force: o.time_in_force,
    });
    return { payload, signature: sign(null, Buffer.from(payload), privateKey).toString("base64") };
  };
  const approveCancel = (clientOrderId: number): Approval => {
    const payload = JSON.stringify({ action: "cancel", client_order_id: clientOrderId, expires_ts_ns: NOW_NS + 5_000_000_000 });
    return { payload, signature: sign(null, Buffer.from(payload), privateKey).toString("base64") };
  };
  return { key, approve, approveCancel };
}

function order(id: number, fields: Partial<ApprovedOrder> = {}): ApprovedOrder {
  return { client_order_id: id, instrument: "SPY", side: "buy", qty: "1", limit_price: "10.00", time_in_force: "gtc", request_id: `dr-${id}`, ...fields };
}

const tempPath = (name: string) => path.join(mkdtempSync(path.join(tmpdir(), "broker-review-")), name);

function gateway(opts: { broker: BrokerAdapter; key: KeyObject; journalPath: string; reporter?: Reporter; clock?: { ns: number }; requestTimeoutMs?: number }) {
  const clock = opts.clock ?? { ns: NOW_NS };
  return new BrokerGateway({
    broker: opts.broker,
    reporter: opts.reporter ?? new Reporter(),
    approvalPublicKey: opts.key,
    now: () => clock.ns,
    requestTimeoutMs: opts.requestTimeoutMs ?? 100,
    iocCancelAfterMs: 1,
    journalPath: opts.journalPath,
  });
}

afterEach(() => vi.restoreAllMocks());

describe("bridge restart: the journal keeps each bridge session's client_order_ids apart", () => {
  it("a new session's order 1 is sent and filled on its own, never answered with the previous session's order 1", async () => {
    const journalPath = tempPath("broker-journal.jsonl");
    const calls: Call[] = [];
    const place: Handler = (a) => venue({ venue_order_id: `venue-${limitOf(a)}`, ref_id: a.refId, status: "filled", filled_qty: "1", avg_price: limitOf(a) });
    const broker = fakeBroker({ previewOrder: () => venue({ warnings: [] }), placeOrder: place }, calls);

    const before = bridgeSession();
    const oldResult = await gateway({ broker, key: before.key, journalPath }).submitOrder(order(1), before.approve(order(1)), "equity");
    const oldRefId = calls.find((c) => c.name === "placeOrder")!.args.refId as string;

    // The bridge restarts: fresh key, next_client_id back at 0, so the next order is 1 again.
    const after = bridgeSession();
    const reporter = new Reporter();
    const restarted = gateway({ broker, key: after.key, journalPath, reporter });
    const newOrder = order(1, { limit_price: "12.00", request_id: "dr-1-new-session" });
    calls.length = 0;

    const result = await restarted.submitOrder(newOrder, after.approve(newOrder), "equity");

    expect(oldResult.avg_price).toBe("10.00");
    expect(calls.filter((c) => c.name === "placeOrder")).toHaveLength(1);
    expect(result).toMatchObject({ status: "filled", avg_price: "12.00" });
    expect(reporter.events()).toEqual([["filled", "1", "12"]]);
    // Reconciliation maps only the current session's ref_ids to client_order_ids.
    expect(restarted.clientOrderIdForRefId(oldRefId)).toBeNull();
    expect(restarted.clientOrderIdForRefId(calls.find((c) => c.name === "placeOrder")!.args.refId as string)).toBe(1);
  });

  it("a previous session's unknown and open orders are neither resent nor cancellable under a new session's ids", async () => {
    const journalPath = tempPath("broker-journal.jsonl");
    const calls: Call[] = [];
    let placeBehaviour: "timeout" | "accept" = "timeout";
    const broker = fakeBroker(
      {
        previewOrder: () => venue({ warnings: [] }),
        placeOrder: (a) => {
          if (placeBehaviour === "timeout") throw new Error("synthetic transport timeout");
          return venue({ venue_order_id: `venue-${String(a.refId)}`, ref_id: a.refId, status: "accepted" });
        },
        findOrderByRefId: () => undefined,
        cancelOrder: () => undefined,
      },
      calls,
    );

    const before = bridgeSession();
    const oldGateway = gateway({ broker, key: before.key, journalPath });
    // Order 2: outcome unknown when the session ends (every send and lookup ambiguous).
    await expect(oldGateway.submitOrder(order(2), before.approve(order(2)), "equity")).rejects.toThrow(/outcome unknown/);
    // Order 3: a gtc order left open at the venue.
    placeBehaviour = "accept";
    await oldGateway.submitOrder(order(3), before.approve(order(3)), "equity");
    const oldRefIds = new Set(calls.filter((c) => c.name === "placeOrder").map((c) => c.args.refId));

    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const after = bridgeSession();
    const restarted = gateway({ broker, key: after.key, journalPath });
    // Visible, never matched: the startup log names both, and neither maps to a client_order_id.
    expect(logged.mock.calls.flat().join(" ")).toMatch(/2 order\(s\) from earlier bridge sessions/);
    for (const refId of oldRefIds) expect(restarted.clientOrderIdForRefId(refId as string)).toBeNull();
    calls.length = 0;

    // A cancel approval for the new session's order 3 never reaches the old session's order 3.
    await expect(restarted.cancelOrder(3, "SPY", "equity", after.approveCancel(3))).rejects.toThrow(GatewayRejection);
    expect(calls).toEqual([]);

    // The new session's order 2 is a first send under its own ref_id, not a lookup-and-resend of the old one.
    const newOrder = order(2, { request_id: "dr-2-new-session" });
    await restarted.submitOrder(newOrder, after.approve(newOrder), "equity");
    const names = calls.map((c) => c.name);
    expect(names).toEqual(["previewOrder", "placeOrder"]);
    expect(oldRefIds.has(calls[1]!.args.refId)).toBe(false);
  });
});

describe("approval expiry around the preview", () => {
  it("does not send when the approval expired while the preview ran", async () => {
    const journalPath = tempPath("broker-journal.jsonl");
    const calls: Call[] = [];
    const clock = { ns: NOW_NS };
    const session = bridgeSession();
    const broker = fakeBroker(
      {
        previewOrder: () => {
          clock.ns += 6_000_000_000; // a slow preview outlives the 5 s approval
          return venue({ warnings: [] });
        },
        placeOrder: (a) => venue({ venue_order_id: "v", ref_id: a.refId, status: "accepted" }),
      },
      calls,
    );
    const g = gateway({ broker, key: session.key, journalPath, clock });

    await expect(g.submitOrder(order(1), session.approve(order(1)), "equity")).rejects.toThrow(/approval expired before the order could be sent/);
    expect(calls.map((c) => c.name)).toEqual(["previewOrder"]);
    // Nothing was sent, so nothing is journaled as sent.
    expect(() => readFileSync(journalPath, "utf8")).toThrow();
  });

  it("gives the preview a shorter timeout than a place call", async () => {
    const calls: Call[] = [];
    const session = bridgeSession();
    const broker = fakeBroker(
      { previewOrder: () => venue({ warnings: [] }), placeOrder: (a) => venue({ venue_order_id: "v", ref_id: a.refId, status: "filled", filled_qty: "1", avg_price: "10" }) },
      calls,
    );
    await gateway({ broker, key: session.key, journalPath: tempPath("j.jsonl"), requestTimeoutMs: 5_000 }).submitOrder(order(1), session.approve(order(1)), "equity");

    expect(calls.map((c) => [c.name, c.timeout])).toEqual([
      ["previewOrder", 1_000],
      ["placeOrder", 5_000],
    ]);
  });
});

describe("emulated IOC whose cancel cannot be confirmed", () => {
  it("reports the fill it last read back before throwing, and only the rest afterwards", async () => {
    const session = bridgeSession();
    const reporter = new Reporter();
    let cancelTakes = false;
    const broker = fakeBroker({
      previewOrder: () => venue({ warnings: [] }),
      placeOrder: (a) => venue({ venue_order_id: "v-ioc", ref_id: a.refId, status: "accepted" }),
      cancelOrder: () => undefined,
      getOrderById: () => venue({ venue_order_id: "v-ioc", status: cancelTakes ? "canceled" : "partially_filled", filled_qty: "1", avg_price: "10.00" }),
    });
    vi.spyOn(console, "error").mockImplementation(() => {});
    const g = gateway({ broker, key: session.key, journalPath: tempPath("j.jsonl"), reporter });
    const ioc = order(1, { qty: "2", time_in_force: "ioc" });

    await expect(g.submitOrder(ioc, session.approve(ioc), "equity")).rejects.toThrow(/cancel not confirmed/);
    expect(reporter.events()).toEqual([
      ["accepted", undefined, undefined],
      ["partially_filled", "1", "10"],
    ]);

    // A halt's cancel later takes: the fill already reported is not reported again.
    cancelTakes = true;
    await expect(g.cancelOrder(1, "SPY", "equity", session.approveCancel(1))).resolves.toMatchObject({ status: "canceled" });
    expect(reporter.events().slice(2)).toEqual([["canceled", undefined, undefined]]);
  }, 10_000);
});
