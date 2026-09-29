#!/usr/bin/env node
// Fake qc-bridge: speaks the same JSON Lines protocol (schemas/decision/v1) as the real Rust
// binary in engine/crates/bridge, so BridgeClient and the loop can be tested and demoed before
// (or without) that binary. Feed source: --scenario <json array of DecisionRequest> for tests,
// or --csv <path> [--limit N] to synthesize one from a replay recording for `just agent-sim`.
// QC_FAKE_BRIDGE_MODE selects a misbehavior for bridge-client tests (out_of_order, a bad
// response, a timeout, ...); default is well-behaved.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";

type Json = Record<string, unknown>;

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
const mode = process.env.QC_FAKE_BRIDGE_MODE ?? "normal";

function fakeModelSha256(): string {
  return createHash("sha256").update("fake-model-v1").digest("hex");
}

// ponytail: a deterministic stand-in for real order-book reconstruction (engine/crates/bridge's
// job, not this test double's). Enough to drive `just agent-sim` end to end before that binary
// exists; point QC_BRIDGE_BIN at the real one once it lands and this file stops being used.
function scenarioFromCsv(csvPath: string, limit: number): Json[] {
  const lines = readFileSync(csvPath, "utf8")
    .split("\n")
    .filter((line) => line && !line.startsWith("#"));
  const snapshot = lines.find((line) => line.startsWith("S,"));
  if (!snapshot) return [];
  const fields = snapshot.split(",");
  const bidLevels = fields[6] ?? "";
  const askLevels = fields[7] ?? "";
  const baseBid = Number(bidLevels.split(";")[0]?.split("@")[0] ?? "0");
  const baseAsk = Number(askLevels.split(";")[0]?.split("@")[0] ?? "0");
  const dataLines = lines.filter((line) => line.startsWith("D,") || line.startsWith("T,"));
  const stride = Math.max(1, Math.floor(dataLines.length / Math.max(limit, 1)));

  const rows: Json[] = [];
  for (let i = 0; rows.length < limit && i * stride < dataLines.length; i++) {
    const tick = (i % 5) - 2; // -2..2 ticks, deterministic in i
    const bestBid = (baseBid + tick * 0.1).toFixed(1);
    const bestAsk = (baseAsk + tick * 0.1).toFixed(1);
    const mid = ((Number(bestBid) + Number(bestAsk)) / 2).toFixed(2);
    const direction = (["down", "flat", "up"] as const)[i % 3]!;
    const probs =
      direction === "down" ? [0.7, 0.2, 0.1] : direction === "up" ? [0.1, 0.2, 0.7] : [0.3, 0.4, 0.3];
    rows.push({
      request_id: `sim-${i}`,
      ts_ns: 1_767_571_200_000_000_000 + i * 1_000_000_000,
      instrument: "SIM-BTC",
      best_bid: bestBid,
      best_ask: bestAsk,
      mid,
      spread_ticks: 1,
      features: {},
      signal: { direction, probs, model_sha256: fakeModelSha256() },
      position: "0",
      limits: {
        max_position: "10",
        max_notional: "100000",
        max_order_rate_per_sec: 5,
        remaining_daily_loss: "1000",
      },
      allowed_actions: ["buy", "sell", "no_trade"],
    });
  }
  return rows;
}

function loadScenario(): Json[] {
  if (args.scenario) return JSON.parse(readFileSync(args.scenario, "utf8")) as Json[];
  if (args.csv) return scenarioFromCsv(args.csv, args.limit ? Number(args.limit) : 20);
  return [];
}

const scenario = loadScenario();
let cursor = 0;
let outOfOrderBuffer: Json[] = [];

function write(obj: Json): void {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

// quiet_next_decision_request holds the first answer for QC_FAKE_BRIDGE_QUIET_MS, like qc-bridge
// --follow in a quiet market; quiet_then_crash exits 1 after that long instead of answering.
const quietMs = Number(process.env.QC_FAKE_BRIDGE_QUIET_MS ?? "0");
let quietDone = false;

function handleNextDecisionRequest(id: string): void {
  if (mode === "timeout_next_decision_request") return; // never respond, on purpose
  if (mode === "quiet_then_crash") {
    setTimeout(() => process.exit(1), quietMs);
    return;
  }
  if (mode === "quiet_next_decision_request" && !quietDone) {
    quietDone = true;
    setTimeout(() => handleNextDecisionRequest(id), quietMs);
    return;
  }
  if (mode === "malformed_json") {
    process.stdout.write("{not valid json\n");
    return;
  }
  if (mode === "invalid_schema") {
    write({ v: 1, id }); // missing required "ok"
    return;
  }
  if (mode === "error_next_decision_request") {
    write({ v: 1, id, ok: false, error: { code: "bad_request", message: "fake bridge error" } });
    return;
  }

  const decision_request = cursor < scenario.length ? scenario[cursor++] : null;
  const response = { v: 1, id, ok: true, decision_request };

  if (mode === "out_of_order") {
    outOfOrderBuffer.push(response);
    if (outOfOrderBuffer.length === 2) {
      write(outOfOrderBuffer[1]!);
      write(outOfOrderBuffer[0]!);
      outOfOrderBuffer = [];
    }
    return;
  }
  write(response);
}

function handle(req: Json): void {
  const id = req.id as string;
  const op = req.op as string;
  switch (op) {
    case "next_decision_request":
      handleNextDecisionRequest(id);
      return;
    case "submit_order_intent":
      write({ v: 1, id, ok: true, result: { accepted: true, client_order_id: cursor } });
      return;
    case "no_trade":
      write({ v: 1, id, ok: true });
      return;
    case "status":
      write({ v: 1, id, ok: true, status: {} });
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
