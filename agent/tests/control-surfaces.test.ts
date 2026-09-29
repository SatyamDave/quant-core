import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { request } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { ControlBridge } from "../src/control/index.js";
import { createControlServer } from "../src/control/http-server.js";
import { handleRpc } from "../src/control/mcp-server.js";

const LIMITS = 'max_notional = "25"\nmax_order_rate_per_sec = 5\n';
const bridge: ControlBridge = {
  hello: async () => ({ v: 1, id: "h", ok: true, hello: { protocol: "1.2", venue_mode: "sim", approval_public_key: "k" } }),
  status: async () => ({ v: 1, id: "s", ok: true, status: { position: "0", halted: null } }),
  submitOrderIntent: async () => ({ v: 1, id: "x", ok: true, result: { accepted: true, client_order_id: 7 } }),
};
const intent = { request_id: "r1", instrument: "SPY", side: "buy", qty: "1", limit_price: "10", time_in_force: "ioc", reason: "t" };

let dir: string;
let opts: { limitsFile: string; killFile: string; ledgerFile: string; bridge: ControlBridge; uiDir: string };
beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "qc-surf-"));
  opts = { limitsFile: path.join(dir, "l.toml"), killFile: path.join(dir, "KILL"), ledgerFile: path.join(dir, "ledger.jsonl"), bridge, uiDir: path.join(dir, "ui") };
  writeFileSync(opts.limitsFile, LIMITS);
  mkdirSync(opts.uiDir);
  writeFileSync(path.join(opts.uiDir, "index.html"), "<h1>ui</h1>");
});

const call = (name: string, args: object) => handleRpc({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args as never } }, opts) as Promise<{ result: { content: { text: string }[]; isError?: boolean } }>;

describe("mcp", () => {
  it("initializes and lists the tools", async () => {
    const init = (await handleRpc({ id: 0, method: "initialize", params: { protocolVersion: "2025-06-18" } }, opts)) as { result: { protocolVersion: string } };
    expect(init.result.protocolVersion).toBe("2025-06-18");
    expect(await handleRpc({ method: "notifications/initialized" }, opts)).toBeUndefined();
    const list = (await handleRpc({ id: 2, method: "tools/list" }, opts)) as { result: { tools: { name: string }[] } };
    expect(list.result.tools.map((t) => t.name)).toEqual(expect.arrayContaining(["get_status", "get_rules", "propose_rule_change", "submit_order_intent", "list_decisions", "engage_kill_switch"]));
  });

  it("tightens, refuses loosening, submits and kills", async () => {
    expect((await call("propose_rule_change", { key: "max_notional", value: "10" })).result.isError).toBeUndefined();
    const loosen = await call("propose_rule_change", { key: "max_notional", value: "1000" });
    expect(loosen.result.isError).toBe(true);
    expect(loosen.result.content[0]!.text).toMatch(/^refused/);
    expect(readFileSync(opts.limitsFile, "utf8")).toContain('max_notional = "10"');
    expect(JSON.parse((await call("submit_order_intent", { intent })).result.content[0]!.text)).toEqual({ accepted: true, client_order_id: 7 });
    expect((await call("submit_order_intent", { intent: { ...intent, side: "sideways" } })).result.isError).toBe(true);
    await call("engage_kill_switch", { reason: "test" });
    expect(existsSync(opts.killFile)).toBe(true);
    expect(JSON.parse((await call("submit_order_intent", { intent })).result.content[0]!.text).halted).toBe("kill_switch");
  });

  it("speaks newline-delimited JSON-RPC over stdio", async () => {
    const child = spawn(process.execPath, ["--import", "tsx", "src/control/mcp-server.ts"], { cwd: path.join(__dirname, ".."), env: { ...process.env, QC_BRIDGE_BIN: "" } });
    const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
    child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" })}\n`);
    const first = JSON.parse((await lines.next()).value as string) as { id: number; result: { tools: unknown[] } };
    child.kill();
    expect(first.id).toBe(1);
    expect(first.result.tools.length).toBeGreaterThanOrEqual(6);
  }, 20_000);
});

describe("http", () => {
  let base: string;
  let close: () => void;
  beforeEach(async () => {
    const server = createControlServer(opts);
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
    close = () => server.close();
  });
  afterEach(() => close());
  const post = (p: string, b: object) => fetch(base + p, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) });

  it("serves status, rules and the UI", async () => {
    expect((await (await fetch(`${base}/api/status`)).json()).mode).toBe("sim");
    expect(await (await fetch(`${base}/api/rules`)).json()).toEqual({ max_notional: "25", max_order_rate_per_sec: "5" });
    expect(await (await fetch(`${base}/`)).text()).toBe("<h1>ui</h1>");
    expect((await fetch(`${base}/..%2f..%2fetc/passwd`)).status).toBe(404);
  });

  it("tightens, refuses loosening with 403, submits, and kills", async () => {
    expect((await post("/api/rules", { key: "max_order_rate_per_sec", value: "2" })).status).toBe(200);
    const loosen = await post("/api/rules", { key: "max_order_rate_per_sec", value: "50" });
    expect(loosen.status).toBe(403);
    expect(readFileSync(opts.limitsFile, "utf8")).toContain("max_order_rate_per_sec = 2");
    expect(await (await post("/api/orders", { intent })).json()).toEqual({ accepted: true, client_order_id: 7 });
    expect((await post("/api/kill-switch", {})).status).toBe(200);
    expect((await (await fetch(`${base}/api/status`)).json()).killSwitch.engaged).toBe(true);
  });

  it("refuses non-JSON posts and foreign Host headers", async () => {
    expect((await fetch(`${base}/api/kill-switch`, { method: "POST", body: "x" })).status).toBe(415);
    expect(existsSync(opts.killFile)).toBe(false);
    const status = await new Promise<number>((resolve, reject) =>
      request(`${base}/api/status`, { headers: { host: "evil.example" } }, (r) => resolve(r.statusCode ?? 0)).on("error", reject).end(),
    );
    expect(status).toBe(403);
  });
});
