// Protocol v1.1/v1.2 types (additive to v1 -- see wave-spec.md "Protocol v1.1", wave2-spec.md
// "Protocol v1.2", and, once bridge-control's lane lands, schemas/decision/v1/README.md's
// "minor version" note). `Approval`/`ApprovalPayload` are hand-written mirrors of the JSON the
// Rust bridge emits (agent/src/types.ts is the canonical, shared definition as of wave 2 --
// exactly one implementation per concern, root CLAUDE.md -- re-exported here so existing
// imports from this file keep working). `CancelApprovalPayload` is new in v1.2.
import type { Approval, ApprovalPayload, CancelApprovalPayload, DecimalString, Side, TimeInForce } from "../types.js";

export type { Approval, ApprovalPayload, CancelApprovalPayload, DecimalString, Side, TimeInForce };

/** The order the gateway is about to submit. Every field here must equal the corresponding
 *  field parsed out of a verified `Approval.payload`, or the door refuses to open. */
export interface ApprovedOrder {
  client_order_id: number;
  instrument: string;
  side: Side;
  qty: DecimalString;
  limit_price: DecimalString;
  time_in_force: TimeInForce;
  request_id: string;
}

export type AssetClass = "equity" | "crypto";

/** One resting price level on one side of a venue's order book: a real Level 2 level (real
 *  size) from the adapter's getBook(), or a single top-of-book level built from a quote when no
 *  L2 data exists for the asset class (agent/src/recorder/poll.ts's `pollBook` fallback). */
export interface BookLevel {
  price: DecimalString;
  qty: DecimalString;
}

export type ExecutionEvent =
  | "accepted"
  | "rejected"
  | "partially_filled"
  | "filled"
  | "canceled"
  | "cancel_rejected";

export interface ReportExecutionOp {
  op: "report_execution";
  execution: {
    client_order_id: number;
    event: ExecutionEvent;
    qty?: DecimalString;
    price?: DecimalString;
    venue_order_id?: string;
    ts_ns: number;
    reason?: string;
  };
}

export interface ReportExecutionAck {
  ok: boolean;
  error?: { code: string; message: string };
}

/**
 * What the gateway needs from qc-bridge to report fills/acks back into its OMS. Implemented by
 * `agent/src/bridge.ts`'s `BridgeClient` (wave 2, gateway-integration): its `reportExecution()`
 * sends the v1.1 `report_execution` op and adapts the response into this shape. Kept as its own
 * narrow interface (rather than callers depending on `BridgeClient` directly) so tests can hand
 * `BrokerGateway` a trivial recording double instead of a real bridge process.
 */
export interface BridgeReporter {
  reportExecution(op: ReportExecutionOp): Promise<ReportExecutionAck>;
}

/** Normalized broker outcome, in our own vocabulary. `raw` keeps whatever the broker actually
 *  returned, for audit -- normalization happens once, in the broker adapter (adapter.ts). */
export interface PlaceOrderResult {
  status: "accepted" | "filled" | "partially_filled" | "rejected" | "canceled";
  venue_order_id?: string;
  filled_qty?: DecimalString;
  avg_price?: DecimalString;
  reason?: string;
  raw: Record<string, unknown>;
}

export interface OrderStatusResult {
  status: PlaceOrderResult["status"] | "not_found";
  venue_order_id?: string;
  filled_qty?: DecimalString;
  avg_price?: DecimalString;
  reason?: string;
  raw: Record<string, unknown> | null;
}

export interface CancelOrderResult {
  status: "canceled" | "cancel_rejected";
  reason?: string;
  raw: Record<string, unknown>;
}

export interface PreviewResult {
  warnings: string[];
  raw: Record<string, unknown>;
}
