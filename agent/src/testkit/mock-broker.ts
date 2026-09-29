// The mock broker: an in-process, synthetic `BrokerAdapter` (agent/src/broker/adapter.ts) with
// scripted order outcomes, used by the tests, `just agent-sim-external`, the canary mock and
// `just record-quotes --dry-run`. No network and no real broker. Every value is SYNTHETIC.
//
// This is deliberately NOT agent/src/testkit/fake-bridge.ts (that fakes qc-bridge). This fakes a
// broker, behind the same interface a real adapter implements.
import { fromFixed, toFixed } from "../decimal.js";
import {
  BookUnsupportedError,
  type AccountState,
  type BookResult,
  type BrokerAdapter,
  type CallOptions,
  type Quote,
  type VenueOrderRecord,
} from "../broker/adapter.js";
import type { ApprovedOrder, AssetClass, OrderStatusResult, PlaceOrderResult, PreviewResult } from "../broker/types.js";

export type MockScenario =
  | "accepted" // rests; a cancel cancels it
  | "filled"
  | "partially_filled" // half filled and still resting; a cancel keeps the half
  | "rejected"
  | "canceled"
  | "timeout" // the broker fills it, but the response never arrives
  | "lost_once" // the first send never arrives and never answers; a resend with the same ref_id fills
  | "never_arrives" // no send ever arrives or answers
  | "cancel_fails_filled" // rests; the cancel errors because the order filled first
  | "cancel_stuck"; // rests; every cancel errors and it stays live

export type MockMethod = keyof BrokerAdapter;

export interface MockCall {
  method: MockMethod;
  args: Record<string, unknown>;
  timeoutMs?: number;
  at: number;
}

export interface MockBrokerOptions {
  /** Placement sequence number ("1" for the first distinct ref_id placed, "2" for the next, ...)
   *  -> scenario. Unlisted orders are "accepted". A resend with a ref_id already seen is the same
   *  order (the venue deduplicates by ref_id), so it keeps its number and scenario. */
  scenarios?: Record<string, MockScenario>;
  /** Invoked on every call, before any scenario logic. */
  onCall?: (call: MockCall) => void;
  /** verifyAccount() rejects with this message (e.g. an account that may not trade). */
  accountError?: string;
  /** getAccountState's position for `positionSymbol` (default "SPY"). Defaults to the mock's own
   *  bookkeeping of every order's signed fills, per symbol. */
  position?: string;
  positionSymbol?: string;
  /** getAccountState's cash; null (not reported) by default. */
  cash?: string | null;
  /** getBook returns empty sides, as a real venue's book can be after hours. */
  emptyBook?: boolean;
}

interface MockOrder {
  id: string;
  ref_id: string;
  scenario: MockScenario;
  symbol: string;
  side: string;
  quantity: string;
  limit_price: string;
  state: PlaceOrderResult["status"];
  filled: bigint; // fixed-point, decimal.ts scale
  reason?: string;
}

/** Never resolves on its own; rejects once the caller's timeout passes, like a real transport. */
function hang<T>(opts: CallOptions | undefined): Promise<T> {
  return new Promise<T>((_, reject) => {
    if (opts?.timeoutMs !== undefined) setTimeout(() => reject(new Error(`SYNTHETIC: request timed out after ${opts.timeoutMs}ms`)), opts.timeoutMs);
  });
}

function record(o: MockOrder): VenueOrderRecord {
  const raw = { id: o.id, ref_id: o.ref_id, state: o.state, symbol: o.symbol, side: o.side, quantity: o.quantity, limit_price: o.limit_price, filled_quantity: fromFixed(o.filled) };
  return {
    status: o.state,
    venue_order_id: o.id,
    ref_id: o.ref_id,
    filled_qty: fromFixed(o.filled),
    avg_price: o.filled > 0n ? o.limit_price : undefined,
    reason: o.reason,
    raw,
  };
}

export class MockBroker implements BrokerAdapter {
  readonly calls: MockCall[] = [];
  private readonly byRefId = new Map<string, MockOrder>();
  private readonly seqByRefId = new Map<string, number>();
  private readonly lostOnceSpent = new Set<string>();

  constructor(private readonly opts: MockBrokerOptions = {}) {}

  private log(method: MockMethod, args: Record<string, unknown>, opts?: CallOptions): void {
    const call = { method, args, timeoutMs: opts?.timeoutMs, at: Date.now() };
    this.calls.push(call);
    this.opts.onCall?.(call);
  }

  callsTo(method: MockMethod): MockCall[] {
    return this.calls.filter((c) => c.method === method);
  }

  private byId(venueOrderId: string): MockOrder | undefined {
    return [...this.byRefId.values()].find((o) => o.id === venueOrderId);
  }

  async verifyAccount(opts?: CallOptions): Promise<void> {
    this.log("verifyAccount", {}, opts);
    if (this.opts.accountError) throw new Error(`SYNTHETIC: ${this.opts.accountError}`);
  }

  async previewOrder(assetClass: AssetClass, order: ApprovedOrder, opts?: CallOptions): Promise<PreviewResult> {
    this.log("previewOrder", { assetClass, order }, opts);
    return { warnings: ["SYNTHETIC: preview only, not a real pre-trade check"], raw: {} };
  }

  async placeOrder(assetClass: AssetClass, order: ApprovedOrder, refId: string, opts?: CallOptions): Promise<VenueOrderRecord> {
    this.log("placeOrder", { assetClass, order, refId }, opts);
    if (!this.seqByRefId.has(refId)) this.seqByRefId.set(refId, this.seqByRefId.size + 1);
    const seq = this.seqByRefId.get(refId)!;
    const scenario = this.opts.scenarios?.[String(seq)] ?? "accepted";
    if (scenario === "never_arrives") return hang(opts);
    if (scenario === "lost_once" && !this.lostOnceSpent.has(refId)) {
      this.lostOnceSpent.add(refId);
      return hang(opts); // never arrived, never answers
    }
    const existing = this.byRefId.get(refId);
    if (existing) return record(existing); // venue dedup by ref_id
    const full = toFixed(order.qty);
    const [state, filled, reason] = ((): [PlaceOrderResult["status"], bigint, string?] => {
      switch (scenario) {
        case "filled":
        case "timeout":
        case "lost_once":
          return ["filled", full];
        case "partially_filled":
          return ["partially_filled", full / 2n];
        case "rejected":
          return ["rejected", 0n, "SYNTHETIC: insufficient buying power"];
        case "canceled":
          return ["canceled", 0n];
        default:
          return ["accepted", 0n];
      }
    })();
    const o: MockOrder = {
      id: `mock-order-${String(seq).padStart(6, "0")}`,
      ref_id: refId,
      scenario,
      symbol: order.instrument,
      side: order.side,
      quantity: fromFixed(full),
      limit_price: fromFixed(toFixed(order.limit_price)),
      state,
      filled,
      reason,
    };
    this.byRefId.set(refId, o);
    // Recorded as if the venue really did get it; only the response back is lost.
    if (scenario === "timeout") return hang(opts);
    return record(o);
  }

  async cancelOrder(assetClass: AssetClass, venueOrderId: string, opts?: CallOptions): Promise<void> {
    this.log("cancelOrder", { assetClass, venueOrderId }, opts);
    const o = this.byId(venueOrderId);
    if (!o) throw new Error("SYNTHETIC: order not found in this account");
    if (o.scenario === "cancel_fails_filled") {
      o.state = "filled";
      o.filled = toFixed(o.quantity);
      throw new Error("SYNTHETIC: order already filled, cannot cancel");
    }
    if (o.scenario === "cancel_stuck") throw new Error("SYNTHETIC: cancel failed, try again");
    if (o.state !== "accepted" && o.state !== "partially_filled") throw new Error(`SYNTHETIC: order is ${o.state}, cannot cancel`);
    o.state = "canceled";
  }

  async getOrderById(assetClass: AssetClass, venueOrderId: string, opts?: CallOptions): Promise<OrderStatusResult> {
    this.log("getOrderById", { assetClass, venueOrderId }, opts);
    const o = this.byId(venueOrderId);
    return o ? record(o) : { status: "not_found", raw: null };
  }

  async findOrderByRefId(assetClass: AssetClass, refId: string, symbol: string, createdAtGte: string, opts?: CallOptions): Promise<VenueOrderRecord | undefined> {
    this.log("findOrderByRefId", { assetClass, refId, symbol, createdAtGte }, opts);
    const o = this.byRefId.get(refId);
    return o && o.symbol === symbol ? record(o) : undefined;
  }

  async listOrders(assetClass: AssetClass, opts?: CallOptions): Promise<VenueOrderRecord[]> {
    this.log("listOrders", { assetClass }, opts);
    return [...this.byRefId.values()].map(record);
  }

  async getAccountState(assetClass: AssetClass, instrument: string, opts?: CallOptions): Promise<AccountState> {
    this.log("getAccountState", { assetClass, instrument }, opts);
    let position: string;
    if (this.opts.position !== undefined) {
      position = (this.opts.positionSymbol ?? "SPY") === instrument ? this.opts.position : "0";
    } else {
      let qty = 0n;
      for (const o of this.byRefId.values()) if (o.symbol === instrument) qty += o.side === "sell" ? -o.filled : o.filled;
      position = fromFixed(qty);
    }
    return { position, cash: this.opts.cash ?? null };
  }

  async getQuote(assetClass: AssetClass, instrument: string, opts?: CallOptions): Promise<Quote> {
    this.log("getQuote", { assetClass, instrument }, opts);
    return { bid: "100.00", ask: "100.05", last: "100.02", raw: { symbol: instrument, source: "SYNTHETIC" } };
  }

  async getBook(assetClass: AssetClass, instrument: string, opts?: CallOptions): Promise<BookResult> {
    this.log("getBook", { assetClass, instrument }, opts);
    if (assetClass !== "equity") throw new BookUnsupportedError(assetClass);
    // SYNTHETIC 3-level book per side, best-first, around the same 100.00/100.05 top as getQuote.
    if (this.opts.emptyBook) return { bids: [], asks: [], raw: { symbol: instrument, source: "SYNTHETIC" } };
    return {
      bids: [
        { price: "99.99", qty: "500" },
        { price: "99.98", qty: "300" },
        { price: "99.97", qty: "200" },
      ],
      asks: [
        { price: "100.05", qty: "400" },
        { price: "100.06", qty: "250" },
        { price: "100.07", qty: "150" },
      ],
      raw: { symbol: instrument, source: "SYNTHETIC" },
    };
  }
}

/** Default export for QC_BROKER_MODULE (agent/src/broker/adapter.ts's loadBrokerAdapter), so the
 *  CLI's external mode can be pointed at the mock for a local dry run. */
export default async function createMockBroker(): Promise<BrokerAdapter> {
  return new MockBroker();
}
