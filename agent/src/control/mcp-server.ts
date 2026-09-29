// MCP stdio server over the control core (src/control/index.ts), so an operator's own agent
// (Claude Code or any MCP client) can read status and rules, tighten a rule, read decisions,
// submit an order intent through qc-bridge's risk checks, and pull the kill switch.
// ponytail: hand-rolled newline-delimited JSON-RPC 2.0 (initialize, ping, tools/list,
// tools/call) instead of @modelcontextprotocol/sdk, which is only a transitive dependency here;
// switch to the SDK if resources, prompts or streaming are ever needed.
//
// There is deliberately no tool that loosens a limit or releases the kill switch: both need a
// human (root CLAUDE.md rules 4 and 11).
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { BridgeClient } from "../bridge.js";
import { validateOrThrow } from "../schema.js";
import type { OrderIntent } from "../types.js";
import {
  type ControlBridge,
  type ControlOptions,
  engageKillSwitch,
  getRules,
  getStatus,
  listDecisions,
  listOrders,
  proposeRuleChange,
  submitOrderIntent,
} from "./index.js";

type Args = Record<string, unknown>;

export interface Operation {
  description: string;
  inputSchema: Record<string, unknown>;
  run: (args: Args, opts: ControlOptions) => unknown;
}

const limitArg = { limit: { type: "integer", minimum: 1, maximum: 1000, description: "Newest N (default 20)." } };

function limitOf(args: Args): number {
  const n = args.limit ?? 20;
  if (!Number.isInteger(n) || (n as number) < 1 || (n as number) > 1000) throw new Error("limit must be an integer 1..1000");
  return n as number;
}

function str(args: Args, key: string): string {
  const v = args[key];
  if (typeof v !== "string" || !v) throw new Error(`${key} must be a non-empty string`);
  return v;
}

/** One table both surfaces (this MCP server and http-server.ts) dispatch through. */
export const OPERATIONS: Record<string, Operation> = {
  get_status: {
    description: "Mode (sim/paper/external), kill switch, halt reason, position, today's decisions/orders/notional, and current limits.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    run: (_a, opts) => getStatus(opts),
  },
  get_rules: {
    description: "Current risk limits from config/limits (decimal strings).",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    run: (_a, opts) => getRules(opts),
  },
  propose_rule_change: {
    description:
      "Tighten one risk limit (applied from the bridge's next restart). Any loosening is refused: only a human with two approvals may loosen a limit.",
    inputSchema: {
      type: "object",
      properties: { key: { type: "string" }, value: { type: "string", description: "Decimal, e.g. \"10\"" } },
      required: ["key", "value"],
      additionalProperties: false,
    },
    run: (a, opts) => proposeRuleChange({ key: str(a, "key"), value: str(a, "value") }, opts),
  },
  submit_order_intent: {
    description:
      "Submit an order intent to qc-bridge's risk-wrapped submit_order_intent (sim/paper by default). Returns the risk verdict; never reaches a broker directly.",
    inputSchema: {
      type: "object",
      properties: { intent: { type: "object", description: "schemas/decision/v1/order_intent.schema.json" } },
      required: ["intent"],
      additionalProperties: false,
    },
    run: async (a, opts) => {
      validateOrThrow("order_intent", a.intent);
      if (!opts.bridge) throw new Error("no bridge: start this server with QC_BRIDGE_BIN (and QC_BRIDGE_ARGS) set");
      // External (real-venue) routing is the operator's switch, never an argument the agent passes.
      const allowExternal = process.env.QC_CONTROL_ALLOW_EXTERNAL === "1";
      return submitOrderIntent(a.intent as OrderIntent, { ...opts, bridge: opts.bridge, allowExternal });
    },
  },
  list_decisions: {
    description: "Decision ledger entries, newest first.",
    inputSchema: { type: "object", properties: limitArg, additionalProperties: false },
    run: (a, opts) => listDecisions(limitOf(a), opts),
  },
  list_orders: {
    description: "Ledger entries whose trade the bridge accepted, newest first.",
    inputSchema: { type: "object", properties: limitArg, additionalProperties: false },
    run: (a, opts) => listOrders(limitOf(a), opts),
  },
  engage_kill_switch: {
    description: "Halt trading now (qc-bridge halts within 1s and cancels working orders). One-way: only a human can release it.",
    inputSchema: { type: "object", properties: { reason: { type: "string" } }, additionalProperties: false },
    run: (a, opts) => engageKillSwitch(typeof a.reason === "string" ? a.reason : undefined, opts),
  },
};

/** A qc-bridge from QC_BRIDGE_BIN/QC_BRIDGE_ARGS, started on first use; none if unset. */
export function bridgeFromEnv(): ControlBridge | undefined {
  const bin = process.env.QC_BRIDGE_BIN;
  if (!bin) return undefined;
  const client = new BridgeClient({ command: bin, args: (process.env.QC_BRIDGE_ARGS ?? "").split(" ").filter(Boolean) });
  let started = false;
  const ensure = () => {
    if (!started) client.start();
    started = true;
    return client;
  };
  return {
    hello: () => ensure().hello(),
    status: () => ensure().status(),
    submitOrderIntent: (i) => ensure().submitOrderIntent(i),
  };
}

interface RpcRequest {
  jsonrpc?: string;
  id?: string | number | null;
  method?: string;
  params?: { name?: string; arguments?: Args; protocolVersion?: string };
}

/** Handles one JSON-RPC message; returns the response, or undefined for a notification. */
export async function handleRpc(msg: RpcRequest, opts: ControlOptions): Promise<object | undefined> {
  const id = msg.id ?? null;
  const ok = (result: unknown) => ({ jsonrpc: "2.0", id, result });
  const fail = (code: number, message: string) => ({ jsonrpc: "2.0", id, error: { code, message } });
  if (msg.id === undefined) return undefined; // notifications (e.g. notifications/initialized)
  switch (msg.method) {
    case "initialize":
      return ok({
        protocolVersion: msg.params?.protocolVersion ?? "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "quant-core-control", version: "0.1.0" },
      });
    case "ping":
      return ok({});
    case "tools/list":
      return ok({
        tools: Object.entries(OPERATIONS).map(([name, o]) => ({ name, description: o.description, inputSchema: o.inputSchema })),
      });
    case "tools/call": {
      const op = OPERATIONS[msg.params?.name ?? ""];
      if (!op) return fail(-32602, `unknown tool ${JSON.stringify(msg.params?.name)}`);
      try {
        const result = await op.run(msg.params?.arguments ?? {}, opts);
        return ok({ content: [{ type: "text", text: JSON.stringify(result, null, 2) }] });
      } catch (err) {
        // Tool errors (including every refused loosening) go back to the model, not as a protocol error.
        return ok({ content: [{ type: "text", text: (err as Error).message }], isError: true });
      }
    }
    default:
      return fail(-32601, `method not found: ${msg.method}`);
  }
}

export function serveStdio(opts: ControlOptions = { bridge: bridgeFromEnv() }): void {
  const write = (o: object) => process.stdout.write(`${JSON.stringify(o)}\n`);
  createInterface({ input: process.stdin }).on("line", (line) => {
    if (!line.trim()) return;
    let msg: RpcRequest;
    try {
      msg = JSON.parse(line) as RpcRequest;
    } catch {
      write({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } });
      return;
    }
    void handleRpc(msg, opts).then((r) => r && write(r));
  });
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) serveStdio();
