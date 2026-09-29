// Hand-written to mirror schemas/decision/v1/*.schema.json (ADR-0040 protocol v1). Both sides
// validate against the JSON Schema files at runtime (src/schema.ts); these types are for
// compile-time checking only and must stay in sync with the schemas.

/** Fixed-point decimal on the wire: `^-?[0-9]+(\.[0-9]{1,8})?$`. Never a JSON number. */
export type DecimalString = string;

export type Direction = "up" | "flat" | "down";
export type Action = "buy" | "sell" | "no_trade";
export type Side = "buy" | "sell";
export type TimeInForce = "ioc" | "gtc";
export type AllowedAction = "buy" | "sell" | "no_trade";

export interface Signal {
  direction: Direction;
  /** [down, flat, up], each 0..1. */
  probs: [number, number, number];
  model_sha256: string;
}

export interface Limits {
  max_position: DecimalString;
  max_notional: DecimalString;
  max_order_rate_per_sec: number;
  remaining_daily_loss: DecimalString;
}

export interface DecisionRequest {
  request_id: string;
  ts_ns: number;
  instrument: string;
  best_bid: DecimalString;
  best_ask: DecimalString;
  mid: DecimalString;
  spread_ticks: number;
  features: Record<string, number>;
  signal: Signal | null;
  position: DecimalString;
  limits: Limits;
  allowed_actions: AllowedAction[];
}

export interface Decision {
  request_id: string;
  action: Action;
  /** Required when action is buy/sell. */
  qty?: DecimalString;
  /** Required when action is buy/sell. */
  limit_price?: DecimalString;
  /** Advisory only; the Rust risk engine decides whether an order is allowed. */
  confidence?: number;
  rationale: string;
}

export interface OrderIntent {
  request_id: string;
  instrument: string;
  side: Side;
  qty: DecimalString;
  limit_price: DecimalString;
  time_in_force: TimeInForce;
  reason: string;
}

/**
 * Protocol v1.1 approval token (issue #34): a short-lived, tamper-proof stamp over exactly the
 * order risk approved, so the gateway can act on risk's decision without re-implementing or
 * trusting it itself. Canonical home for this type -- agent/src/broker/types.ts re-exports it
 * rather than redefining it, so there is exactly one definition (root CLAUDE.md "one
 * implementation per concern").
 */
export interface Approval {
  /** Canonical JSON string, keys sorted, no whitespace -- exactly the bytes that were signed. */
  payload: string;
  /** Base64 Ed25519 signature over the UTF-8 bytes of `payload`. */
  signature: string;
}

/** `Approval.payload`, parsed: schemas/decision/v1/README.md's v1.1 section, keys already
 *  alphabetical. */
export interface ApprovalPayload {
  client_order_id: number;
  /** u64 nanoseconds on the wire. Parsed with plain JSON.parse, which rounds to the nearest
   *  double once the value exceeds 2^53 -- for a wall-clock nanosecond epoch timestamp that's
   *  an error of a few hundred ns at most (ULP near 1.7e18 is ~400ns), utterly negligible
   *  against a 5-second TTL.
   *  ponytail: bounded precision loss, fine down to microsecond TTLs; if the bridge ever issues
   *  approvals with sub-microsecond TTLs, switch to a bigint-preserving JSON parse. */
  expires_ts_ns: number;
  instrument: string;
  limit_price: DecimalString;
  qty: DecimalString;
  request_id: string;
  side: Side;
  time_in_force: TimeInForce;
}

/** Protocol v1.2 `cancel_order_intent` approval payload (wave2-spec.md): a distinct, smaller
 *  payload shape from `ApprovalPayload` above -- a cancel approves stopping an order, not a new
 *  order's fields, so it carries only what a cancel needs. Signed with the same Ed25519 key.
 *  `reason` is v1.2.1's additive field (issue #44's cancel-on-halt gap): present as `"halt"` only
 *  when the bridge minted this cancel itself as part of halting (as opposed to an explicit
 *  `cancel_order_intent` call, which carries no `reason` at all, byte-for-byte unchanged from
 *  before this field existed) -- audit-only, never checked by verification. */
export interface CancelApprovalPayload {
  action: "cancel";
  client_order_id: number;
  expires_ts_ns: number;
  reason?: "halt";
}

/** One order the bridge halted and is still waiting on the gateway to finish cancelling at the
 *  real broker (protocol v1.2.1, issue #44's cancel-on-halt gap): a fresh, unexpired cancel
 *  approval, minted on demand every time `status`/`drain_halt_cancels` is called so it is never
 *  stale by the time the gateway's heartbeat acts on it. */
export interface PendingCancel {
  client_order_id: number;
  approval: Approval;
}

/** `status` op's result shape (loosely typed beyond what callers actually read: the bridge is
 *  free to add fields additively). `pending_cancels` is protocol v1.2.1; every other field is
 *  v1.2 or earlier. */
export interface BridgeStatus {
  position?: DecimalString;
  halted?: string | null;
  reconciled?: boolean;
  /** v1.2+: the latest feed/market timestamp (ns) this process has seen. */
  market_ts_ns?: number;
  open_orders?: Array<{
    client_order_id: number;
    side: Side;
    price: DecimalString;
    qty: DecimalString;
    filled: DecimalString;
    state: string;
  }>;
  pending_cancels?: PendingCancel[];
}

export interface IntentResult {
  accepted: boolean;
  client_order_id?: number;
  risk_reject?: string;
  halted?: string;
  /** Present only in `external` venue mode on acceptance (protocol v1.1): the gateway must
   *  verify this before ever calling the broker. */
  approval?: Approval;
}

/** Protocol v1.1's `hello` response (schemas/decision/v1/hello.schema.json), extended by v1.2
 *  with `market_ts_ns` (wave2-spec.md: "hello and status gain market_ts_ns"). `protocol` is
 *  typed as a plain string, not a literal union: the shared schema file
 *  (schemas/decision/v1/hello.schema.json, bridge-control's lane) still pins it to the literal
 *  "1.1" as of this PR, so a real v1.2 bridge's response is validated by hand in bridge.ts
 *  rather than against that schema -- see bridge.ts's "V12_ONLY" comment. */
export interface Hello {
  protocol: string;
  venue_mode: "sim" | "external";
  approval_public_key: string;
  /** v1.2+: the latest feed/market timestamp (ns) the bridge has seen. Absent from a v1.1-only
   *  bridge. */
  market_ts_ns?: number;
}

export type ExecutionEvent =
  | "accepted"
  | "rejected"
  | "partially_filled"
  | "filled"
  | "canceled"
  | "cancel_rejected";

/** Protocol v1.1 `report_execution` op payload (gateway -> bridge, external mode). */
export interface OrderExecution {
  client_order_id: number;
  event: ExecutionEvent;
  qty?: DecimalString;
  price?: DecimalString;
  venue_order_id?: string;
  ts_ns: number;
  reason?: string;
}

/** One order as the venue itself reports it, for protocol v1.2's `reconcile` op. */
export interface VenueOrderSnapshot {
  client_order_id: number | null;
  venue_order_id: string;
  state: "open" | "filled" | "canceled" | "rejected";
  filled_qty: DecimalString;
}

export interface VenueSnapshot {
  orders: VenueOrderSnapshot[];
  position: DecimalString;
  cash: DecimalString | null;
}

export type BridgeOp =
  | "next_decision_request"
  | "submit_order_intent"
  | "no_trade"
  | "status"
  | "shutdown"
  // Protocol v1.1 (additive; schemas/decision/v1/README.md):
  | "hello"
  | "report_execution"
  // Protocol v1.2 (additive; wave2-spec.md).
  | "cancel_order_intent"
  | "reconcile"
  // Protocol v1.2.1 (additive; issue #44's cancel-on-halt gap): pulls a fresh cancel approval
  // for every order the bridge is still waiting on the gateway to finish cancelling at the real
  // broker (see PendingCancel/BridgeStatus.pending_cancels above).
  | "drain_halt_cancels";

export interface BridgeRequest {
  v: 1;
  id: string;
  op: BridgeOp;
  intent?: OrderIntent;
  request_id?: string;
  reason?: string;
  /** report_execution */
  execution?: OrderExecution;
  /** cancel_order_intent */
  client_order_id?: number;
  /** reconcile */
  venue?: VenueSnapshot;
}

export interface BridgeError {
  code: string;
  message: string;
}

export interface BridgeResponse {
  v: 1;
  id: string;
  ok: boolean;
  decision_request?: DecisionRequest | null;
  result?: IntentResult;
  status?: BridgeStatus;
  error?: BridgeError;
  /** hello */
  hello?: Hello;
  /** reconcile (protocol v1.2): the envelope's own `ok` above only reflects whether the request
   *  was well-formed -- a successful comparison that found a mismatch still answers with the
   *  envelope's `ok:true` and puts the actual clean/mismatch verdict here. Callers must read
   *  `reconcile.ok`/`reconcile.discrepancies`, never the envelope's `ok`, to learn whether
   *  reconciliation was clean (schemas/decision/v1/README.md's v1.2 section). */
  reconcile?: { ok: boolean; discrepancies: string[] };
  /** drain_halt_cancels (protocol v1.2.1): same shape as `status.pending_cancels` above. */
  pending_cancels?: PendingCancel[];
}

export type LedgerMode = "fake" | "live" | "replay";

export interface DecisionLedgerEntry {
  request: DecisionRequest;
  decision: Decision;
  result: IntentResult | null;
  mode: LedgerMode;
  prompt_version: string;
  model: string;
  /** Full SDK request/response in live mode; null in fake mode. */
  raw?: unknown;
  /** Decimal string (root CLAUDE.md: no floats for money), like every other money field on the
   *  wire -- wave2 fixes what was previously a JS number here (scripts/ops/alerts.py's docstring
   *  used to flag this as the one exception; it no longer is). */
  cost_usd?: DecimalString;
  latency_ms?: number;
}
