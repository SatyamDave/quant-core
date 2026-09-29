#!/usr/bin/env node
// Fake qc-bridge speaking protocol v1.2 (wave2-spec.md), for this lane's own tests and
// `just agent-sim-external` -- lane A (wave2/bridge-control) owns the real Rust implementation
// of these ops; until that PR merges (or for a plain unit test that doesn't need the real
// binary), this is what BridgeClient's v1.2 methods talk to. Deliberately a separate file from
// agent/src/testkit/fake-bridge.ts (that one is v1/v1.1 only, and is a different lane's shared
// file) -- once wave2/bridge-control merges, point QC_BRIDGE_BIN at the real binary instead and
// this file stops being needed for anything but this lane's own unit tests.
//
// Ops: hello, next_decision_request, submit_order_intent, no_trade, status, report_execution,
// cancel_order_intent, reconcile, shutdown. `--venue sim|external` (default sim, matching the
// real bridge's own default). client_order_id is assigned sequentially (1, 2, 3, ...) in the
// order intents are accepted, so a test/script driving this fake can predict which id an order
// will get.
import { createPrivateKey, createPublicKey, sign as cryptoSign } from "node:crypto";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

type Json = Record<string, unknown>;
type VenueMode = "sim" | "external";

function parseArgs(argv: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg?.startsWith("--")) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      const hasValue = next !== undefined && !next.startsWith("--");
      out[key] = hasValue ? next! : "true";
      if (hasValue) i++;
    }
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
const mode = process.env.QC_FAKE_BRIDGE_V12_MODE ?? "normal";
const venueMode: VenueMode = args.venue === "external" ? "external" : "sim";

function loadScenario(): Json[] {
  if (args.scenario) return JSON.parse(readFileSync(args.scenario, "utf8")) as Json[];
  return [];
}

const scenario = loadScenario();
let cursor = 0;
let clientOrderIdCounter = 0;
let marketTsNs = 0;
let reconciled = false; // external mode refuses new intents until the first clean reconcile
let internalPositionFixed = 0n; // scale 1e8, matching agent/src/decimal.ts's convention

// Fixed, non-secret, test-only seed (this repo already has this exact convention for the same
// reason: schemas/decision/v1/approval-test-vector.json's seed [7;32]) -- deliberately NOT a
// fresh keypair per process. Ed25519 signing is deterministic given the same key and message, so
// a fixed key here is what makes `just agent-sim-external` produce byte-identical ledger and
// journal hashes across two independent runs (identical signatures over identical payloads). The
// REAL qc-bridge generates a fresh keypair every process on purpose (schemas/decision/v1/
// README.md: "never persisted, so an old approval cannot outlive the process that minted it") --
// that determinism guarantee is about THIS fake, not a claim about the real bridge's own
// behavior.
const ED25519_SEED = Buffer.alloc(32, 9);
const PKCS8_ED25519_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");
const privateKey = createPrivateKey({ key: Buffer.concat([PKCS8_ED25519_PREFIX, ED25519_SEED]), format: "der", type: "pkcs8" });
const publicKey = createPublicKey(privateKey);
const rawPublicKey = (publicKey.export({ type: "spki", format: "der" }) as Buffer).subarray(-32);

type OrderState = "PendingNew" | "Open" | "PartiallyFilled" | "Filled" | "Canceled" | "Rejected" | "PendingCancel";

interface OmsOrder {
  client_order_id: number;
  instrument: string;
  side: "buy" | "sell";
  qty: string;
  filled_qty: string;
  state: OrderState;
  venue_order_id?: string;
}

const oms = new Map<number, OmsOrder>();

function write(obj: Json): void {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function toFixed8(value: string): bigint {
  const [whole, frac = ""] = value.replace("-", "").split(".");
  const sign = value.startsWith("-") ? -1n : 1n;
  return sign * (BigInt(whole || "0") * 100_000_000n + BigInt(frac.padEnd(8, "0") || "0"));
}

function canonicalJson(obj: Record<string, unknown>): string {
  const sortedKeys = Object.keys(obj).sort();
  const sorted: Record<string, unknown> = {};
  for (const k of sortedKeys) sorted[k] = obj[k];
  return JSON.stringify(sorted);
}

function signApproval(payloadObj: Record<string, unknown>): { payload: string; signature: string } {
  const payload = canonicalJson(payloadObj);
  const signature = cryptoSign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64");
  return { payload, signature };
}

function handleHello(id: string): void {
  write({
    v: 1,
    id,
    ok: true,
    hello: {
      protocol: "1.2",
      venue_mode: venueMode,
      approval_public_key: rawPublicKey.toString("base64"),
      market_ts_ns: marketTsNs,
    },
  });
}

function handleNextDecisionRequest(id: string): void {
  if (mode === "crash_after_first_decision" && cursor >= 1) {
    // Simulates the bridge process dying unexpectedly mid-run (agent/tests/cli-exit.test.ts):
    // no response at all, then the process itself is gone -- BridgeClient's `child.on("exit",
    // ...)` is what the agent service must react to.
    process.exit(1);
  }
  const decision_request = cursor < scenario.length ? scenario[cursor++] : null;
  if (decision_request) {
    const ts = (decision_request as { ts_ns?: number }).ts_ns;
    if (typeof ts === "number" && ts > marketTsNs) marketTsNs = ts;
  }
  write({ v: 1, id, ok: true, decision_request });
}

function handleSubmitOrderIntent(id: string, req: Json): void {
  const intent = req.intent as
    | { request_id: string; instrument: string; side: "buy" | "sell"; qty: string; limit_price: string; time_in_force: "ioc" | "gtc" }
    | undefined;
  if (!intent) {
    write({ v: 1, id, ok: false, error: { code: "bad_request", message: "submit_order_intent requires intent" } });
    return;
  }
  if (venueMode === "external" && !reconciled) {
    write({ v: 1, id, ok: true, result: { accepted: false, risk_reject: "unreconciled" } });
    return;
  }
  clientOrderIdCounter += 1;
  const clientOrderId = clientOrderIdCounter;

  if (venueMode === "sim") {
    // Sim mode is unchanged from v1: routes straight through, no approval. Not this lane's focus
    // (agent-sim already exercises sim mode against the real binary), kept minimal.
    oms.set(clientOrderId, {
      client_order_id: clientOrderId,
      instrument: intent.instrument,
      side: intent.side,
      qty: intent.qty,
      filled_qty: "0",
      state: "Open",
    });
    write({ v: 1, id, ok: true, result: { accepted: true, client_order_id: clientOrderId } });
    return;
  }

  oms.set(clientOrderId, {
    client_order_id: clientOrderId,
    instrument: intent.instrument,
    side: intent.side,
    qty: intent.qty,
    filled_qty: "0",
    state: "PendingNew",
  });
  const approval = signApproval({
    client_order_id: clientOrderId,
    expires_ts_ns: marketTsNs + 5_000_000_000,
    instrument: intent.instrument,
    limit_price: intent.limit_price,
    qty: intent.qty,
    request_id: intent.request_id,
    side: intent.side,
    time_in_force: intent.time_in_force,
  });
  write({ v: 1, id, ok: true, result: { accepted: true, client_order_id: clientOrderId, approval } });
}

function handleReportExecution(id: string, req: Json): void {
  const execution = req.execution as
    | { client_order_id: number; event: string; qty?: string; price?: string; venue_order_id?: string }
    | undefined;
  if (!execution) {
    write({ v: 1, id, ok: false, error: { code: "bad_request", message: "report_execution requires execution" } });
    return;
  }
  const order = oms.get(execution.client_order_id);
  if (!order) {
    write({ v: 1, id, ok: false, error: { code: "unknown_order", message: `no such client_order_id ${execution.client_order_id}` } });
    return;
  }
  const signedQty = order.side === "buy" ? 1n : -1n;
  switch (execution.event) {
    case "accepted":
      order.state = "Open";
      break;
    case "rejected":
      order.state = "Rejected";
      break;
    case "partially_filled":
      order.state = "PartiallyFilled";
      if (execution.qty) {
        internalPositionFixed += signedQty * toFixed8(execution.qty);
        order.filled_qty = execution.qty;
      }
      break;
    case "filled":
      order.state = "Filled";
      if (execution.qty) {
        // The delta since the last reported fill, not the cumulative total, is what moves
        // position -- a partial fill may already have been applied above.
        const already = toFixed8(order.filled_qty);
        const total = toFixed8(execution.qty);
        internalPositionFixed += signedQty * (total - already);
        order.filled_qty = execution.qty;
      }
      break;
    case "canceled":
      order.state = "Canceled";
      break;
    case "cancel_rejected":
      order.state = order.state === "PendingCancel" ? "Open" : order.state;
      break;
    default:
      write({ v: 1, id, ok: false, error: { code: "illegal_order_event", message: `unrecognized event ${execution.event}` } });
      return;
  }
  if (execution.venue_order_id) order.venue_order_id = execution.venue_order_id;
  write({ v: 1, id, ok: true });
}

function handleCancelOrderIntent(id: string, req: Json): void {
  const clientOrderId = req.client_order_id as number | undefined;
  const order = clientOrderId === undefined ? undefined : oms.get(clientOrderId);
  if (!order || !["Open", "PartiallyFilled", "PendingNew"].includes(order.state)) {
    write({ v: 1, id, ok: false, error: { code: "unknown_order", message: `no live order ${clientOrderId}` } });
    return;
  }
  if (venueMode === "sim") {
    order.state = "Canceled";
    write({ v: 1, id, ok: true, result: { accepted: true, client_order_id: clientOrderId } });
    return;
  }
  order.state = "PendingCancel";
  const approval = signApproval({ action: "cancel", client_order_id: clientOrderId, expires_ts_ns: marketTsNs + 5_000_000_000 });
  write({ v: 1, id, ok: true, result: { accepted: true, client_order_id: clientOrderId, approval } });
}

function handleReconcile(id: string, req: Json): void {
  const venue = req.venue as { orders?: Array<{ client_order_id: number | null; state: string; filled_qty: string }>; position?: string } | undefined;
  const venueOrders = venue?.orders ?? [];
  const diffs: string[] = [];

  for (const order of oms.values()) {
    if (order.state === "PendingNew" || order.state === "PendingCancel") continue; // in-flight, not yet expected at the venue
    const match = venueOrders.find((v) => v.client_order_id === order.client_order_id);
    const expectedState = order.state === "Open" ? "open" : order.state === "PartiallyFilled" ? "open" : order.state.toLowerCase();
    if (!match) {
      diffs.push(`client_order_id ${order.client_order_id}: our OMS has ${order.state}, venue has no matching order`);
      continue;
    }
    if (match.state !== expectedState) {
      diffs.push(`client_order_id ${order.client_order_id}: our OMS has ${order.state}, venue reports ${match.state}`);
    }
  }

  const venuePosition = venue?.position !== undefined ? toFixed8(venue.position) : internalPositionFixed;
  if (venuePosition !== internalPositionFixed) {
    diffs.push(`position mismatch: our OMS has ${internalPositionFixed}, venue reports ${venuePosition}`);
  }

  // Matches the real qc-bridge's wire shape (schemas/decision/v1/README.md's v1.2 section,
  // wire::reconcile): the RPC envelope's own "ok" reflects only whether the request itself was
  // well-formed -- a successful comparison always answers ok:true here, whether or not it found a
  // mismatch. The clean/mismatch verdict is nested under "reconcile". (Wave-2 review finding:
  // this fake used to put the verdict at the envelope's top level instead, which meant a caller
  // written against the real bridge's response shape would silently never observe a mismatch
  // through this fake.)
  if (diffs.length > 0) {
    write({ v: 1, id, ok: true, reconcile: { ok: false, discrepancies: diffs } });
    return;
  }
  reconciled = true;
  write({ v: 1, id, ok: true, reconcile: { ok: true, discrepancies: [] } });
}

function handle(req: Json): void {
  const id = req.id as string;
  const op = req.op as string;
  switch (op) {
    case "hello":
      handleHello(id);
      return;
    case "next_decision_request":
      handleNextDecisionRequest(id);
      return;
    case "submit_order_intent":
      handleSubmitOrderIntent(id, req);
      return;
    case "no_trade":
      write({ v: 1, id, ok: true });
      return;
    case "status":
      write({ v: 1, id, ok: true, status: { market_ts_ns: marketTsNs, reconciled } });
      return;
    case "report_execution":
      handleReportExecution(id, req);
      return;
    case "cancel_order_intent":
      handleCancelOrderIntent(id, req);
      return;
    case "reconcile":
      handleReconcile(id, req);
      return;
    case "shutdown":
      write({ v: 1, id, ok: true });
      process.exit(0);
      return;
    default:
      write({ v: 1, id, ok: false, error: { code: "unknown_op", message: `unknown op ${op}` } });
  }
}

createInterface({ input: process.stdin }).on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  try {
    handle(JSON.parse(trimmed) as Json);
  } catch {
    // Malformed input from the client isn't this fake's concern; a real bridge would error too.
  }
});
