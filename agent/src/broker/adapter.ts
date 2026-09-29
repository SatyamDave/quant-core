// The generic external-broker interface. BrokerGateway (gateway.ts), reconcile.ts, the heartbeat
// and the recorder talk to a broker ONLY through `BrokerAdapter`; a broker-specific adapter is the
// one file that knows a venue's API, credentials and response shapes (see README.md in this
// directory). The in-repo implementation is the synthetic mock broker
// (agent/src/testkit/mock-broker.ts); no real broker adapter ships with this repository.
import { createHash } from "node:crypto";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { fromFixed, toFixed } from "../decimal.js";
import type {
  ApprovedOrder,
  AssetClass,
  BookLevel,
  DecimalString,
  OrderStatusResult,
  PlaceOrderResult,
  PreviewResult,
} from "./types.js";

export interface CallOptions {
  /** Per-call request timeout in ms. A call that exceeds it must reject; the gateway treats that
   *  as "outcome unknown" and looks the order up before ever sending it again. */
  timeoutMs?: number;
}

/** A venue order as the adapter normalized it. `ref_id` is the idempotency key the gateway sent. */
export interface VenueOrderRecord extends PlaceOrderResult {
  ref_id?: string;
}

/** Position and cash for reconciliation. `cash` is null when the venue does not report it. */
export interface AccountState {
  position: DecimalString;
  cash: DecimalString | null;
}

export interface Quote {
  bid: DecimalString;
  ask: DecimalString;
  last: DecimalString;
  raw: Record<string, unknown>;
}

export interface BookResult {
  bids: BookLevel[];
  asks: BookLevel[];
  raw: Record<string, unknown>;
}

/**
 * Everything the order path needs from a broker. Rules every implementation must keep:
 * - Never guess: an unexpected or ambiguous venue response throws, it is never read as a default.
 * - `placeOrder` sends `refId` as the venue's idempotency key, so a resend of the same approved
 *   order is deduplicated upstream. Only limit orders are ever sent.
 * - Reads (`getOrderById`, `findOrderByRefId`, `listOrders`) report the venue's own state; the
 *   gateway never trusts a cancel call's response, only the state read back afterwards.
 * - Account identifiers and credentials stay inside the adapter: never in errors, logs or `raw`.
 */
export interface BrokerAdapter {
  /** Startup check: the configured account exists and may trade. Throws otherwise. */
  verifyAccount(opts?: CallOptions): Promise<void>;
  previewOrder(assetClass: AssetClass, order: ApprovedOrder, opts?: CallOptions): Promise<PreviewResult>;
  placeOrder(assetClass: AssetClass, order: ApprovedOrder, refId: string, opts?: CallOptions): Promise<VenueOrderRecord>;
  /** Sends the cancel. Callers never read its outcome: the order state read back afterwards is
   *  what says whether the cancel took. */
  cancelOrder(assetClass: AssetClass, venueOrderId: string, opts?: CallOptions): Promise<void>;
  getOrderById(assetClass: AssetClass, venueOrderId: string, opts?: CallOptions): Promise<OrderStatusResult>;
  /** After an ambiguous submit: the order carrying `refId` for `symbol` placed since
   *  `createdAtGte` (YYYY-MM-DD), or undefined when none does. */
  findOrderByRefId(assetClass: AssetClass, refId: string, symbol: string, createdAtGte: string, opts?: CallOptions): Promise<VenueOrderRecord | undefined>;
  /** Every order this system placed in the account, for reconciliation. */
  listOrders(assetClass: AssetClass, opts?: CallOptions): Promise<VenueOrderRecord[]>;
  getAccountState(assetClass: AssetClass, instrument: string, opts?: CallOptions): Promise<AccountState>;
  getQuote(assetClass: AssetClass, instrument: string, opts?: CallOptions): Promise<Quote>;
  /** Level 2 book, best level first. Throws BookUnsupportedError when the venue has none. */
  getBook(assetClass: AssetClass, instrument: string, opts?: CallOptions): Promise<BookResult>;
}

/** The read-only market-data half of an adapter: all the recorder is ever handed. */
export type MarketData = Pick<BrokerAdapter, "getQuote" | "getBook">;

/** Thrown by getBook() for an asset class with no Level 2 data. The recorder falls back to a
 *  top-of-book quote on exactly this error. */
export class BookUnsupportedError extends Error {
  constructor(public readonly assetClass: AssetClass) {
    super(`no Level 2 book for asset class "${assetClass}"`);
    this.name = "BookUnsupportedError";
  }
}

// UUIDv5 namespace for ref_id. Fixed forever: changing it changes every ref_id, which would let a
// retry of an already-placed order through as a new one.
const REF_ID_NAMESPACE = Buffer.from("6f1c1d0e8a3b4c7e9d2f5a6b7c8d9e0f", "hex");

/** The idempotency key: a UUIDv5 of the bridge's signed approval payload, so every retry of one
 *  approved order sends the same ref_id and the venue deduplicates it. */
export function refIdForApproval(approvalPayload: string): string {
  const hash = createHash("sha1").update(REF_ID_NAMESPACE).update(approvalPayload, "utf8").digest();
  hash[6] = (hash[6]! & 0x0f) | 0x50; // version 5
  hash[8] = (hash[8]! & 0x3f) | 0x80; // RFC 4122 variant
  const h = hash.subarray(0, 16).toString("hex");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
}

/** qc-bridge's VenueOrderId is a u64 (engine/crates/bridge/src/wire.rs) and a venue's order id is
 *  usually a string. The bridge only needs a stable id per order, so it gets the first 8 bytes
 *  of the id's sha256 as a decimal string; the journal keeps the real id for cancels and lookups. */
export function bridgeVenueOrderId(venueOrderId: string): string {
  return createHash("sha256").update(venueOrderId, "utf8").digest().readBigUInt64BE(0).toString();
}

const ONE_SHARE = toFixed("1");
const ONE_DOLLAR = toFixed("1");
const CENT = toFixed("0.01");
const HUNDREDTH_CENT = toFixed("0.0001");

/** Why the gateway refuses to send this order, or undefined. Whole-share limit orders only, priced
 *  on the penny tick at or above $1 and the 1/100-cent tick below it (SEC Rule 612). Checked before
 *  anything is sent, so an order a venue would refuse is never an ambiguous outcome. */
export function orderRefusal(order: ApprovedOrder): string | undefined {
  let qty: bigint;
  let price: bigint;
  try {
    qty = toFixed(order.qty);
    price = toFixed(order.limit_price);
  } catch {
    return "qty or limit_price is not a decimal string";
  }
  if (qty <= 0n || qty % ONE_SHARE !== 0n) return `qty ${order.qty} is not a positive whole number of shares`;
  if (price <= 0n) return `limit_price ${order.limit_price} is not positive`;
  const tick = price >= ONE_DOLLAR ? CENT : HUNDREDTH_CENT;
  if (price % tick !== 0n) return `limit_price ${order.limit_price} is not on the ${fromFixed(tick)} tick`;
  return undefined;
}

/** The env var naming the module that builds the real broker adapter for external mode
 *  (agent/src/cli.ts). The module's default export is `() => Promise<BrokerAdapter>`. */
export const BROKER_MODULE_ENV = "QC_BROKER_MODULE";

/** Loads the operator's adapter. Fails closed: no module configured, no external mode. */
export async function loadBrokerAdapter(env: NodeJS.ProcessEnv = process.env): Promise<BrokerAdapter> {
  const modulePath = env[BROKER_MODULE_ENV];
  if (!modulePath) throw new Error(`${BROKER_MODULE_ENV} is not set: refusing to start external mode without a broker adapter`);
  const mod = (await import(pathToFileURL(path.resolve(modulePath)).href)) as { default?: unknown };
  if (typeof mod.default !== "function") throw new Error(`${BROKER_MODULE_ENV} module has no default-exported adapter factory`);
  return (await (mod.default as () => Promise<BrokerAdapter>)()) as BrokerAdapter;
}
