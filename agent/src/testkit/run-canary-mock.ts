#!/usr/bin/env node
// `just live-canary SYMBOL --mock` (scripts/ops/live_canary.py): the live canary's trading chain
// end to end with every outside party mocked. No real broker, no real OpenRouter, no key.
//
//   instrument file written by scripts/ops/new_equity_instrument.py (a temp copy of config/)
//   -> a growing recording -> the REAL qc-bridge --follow --venue external --instrument
//      --kill-file --decide-every 1
//   -> OpenRouterDecider against a local mock chat-completions server
//   -> approved intent -> BrokerGateway -> mock broker (place, fill, emulated-IOC rest)
//   -> report_execution -> ledger + broker journal
//   -> kill file -> the bridge halts -> the heartbeat cancels the resting order at the mock.
//
// Same wiring as agent/src/cli.ts's external mode (createExternalModeGateway, runLoop, heartbeat),
// assembled here the way run-external-sim.ts does, because cli.ts deliberately has no way to point
// at a mock broker or a mock model endpoint. The recording's timestamps sit inside a regular NYSE
// session (2026-09-29 10:00 ET) so the instrument's real trading-hours and calendar gate applies.
import { execFileSync } from "node:child_process";
import { appendFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, type Server as HttpServer } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { BridgeClient } from "../bridge.js";
import { createExternalModeGateway } from "../broker/external-mode.js";
import { OpenRouterDecider } from "../decider/openrouter.js";
import { runLoop } from "../loop.js";
import { repoRoot } from "../paths.js";
import { MockBroker } from "./mock-broker.js";
import { resolveRealBridgeBin } from "./real-bridge-bin.js";

/** SYNTHETIC ticker: valid for new_equity_instrument.py, not a real listing we trade. */
export const MOCK_SYMBOL = "MOCKX";
const SESSION_START_NS = BigInt(Date.UTC(2026, 8, 29, 14, 0, 0)) * 1_000_000n; // 10:00 ET

export interface CanaryMockResult {
  ledgerPath: string;
  journalPath: string;
  placeCalls: number;
  cancelCalls: number;
  filledPosition: string | undefined;
  haltedReason: string | undefined;
  openOrdersAfterKill: number;
  mockModelCalls: number;
}

/** A chat-completions stand-in that always proposes buying 1 share at the request's best ask. */
async function startMockOpenRouter(): Promise<{ server: HttpServer; endpoint: string; calls: () => number }> {
  let calls = 0;
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      calls += 1;
      const prompt = String((JSON.parse(body) as { messages: Array<{ content: string }> }).messages[0]?.content);
      // The prompt template itself mentions these keys; the request JSON comes last.
      const last = (key: string) => [...prompt.matchAll(new RegExp(`"${key}":\\s*"([^"]+)"`, "g"))].at(-1)?.[1];
      const requestId = last("request_id");
      const bestAsk = last("best_ask");
      const decision = { request_id: requestId, action: "buy", qty: "1", limit_price: bestAsk, confidence: 0.6, rationale: "SYNTHETIC mock reply" };
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ choices: [{ message: { role: "assistant", content: JSON.stringify(decision) } }], usage: { cost: 0 } }));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return { server, endpoint: `http://127.0.0.1:${port}/api/v1/chat/completions`, calls: () => calls };
}

/** Copies the committed instrument/limits/calendar configs and runs the real generator on the
 *  copy, unless the symbol's file is already committed, in which case that file is used as is. */
function generateInstrument(dir: string, symbol: string): string {
  const config = path.join(dir, "config");
  cpSync(path.join(repoRoot(), "config", "instruments"), path.join(config, "instruments"), { recursive: true });
  cpSync(path.join(repoRoot(), "config", "limits"), path.join(config, "limits"), { recursive: true });
  const target = path.join(config, "instruments", `${symbol.toLowerCase()}.toml`);
  if (!existsSync(target)) {
    execFileSync("python3", [path.join(repoRoot(), "scripts", "ops", "new_equity_instrument.py"), symbol, "--config-dir", config], {
      stdio: "inherit",
    });
  }
  return target;
}

async function waitFor(what: string, check: () => Promise<boolean> | boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`timed out waiting for ${what}`);
}

function lineCount(file: string): number {
  return existsSync(file) ? readFileSync(file, "utf8").split("\n").filter(Boolean).length : 0;
}

export async function runCanaryMock(
  outDir = mkdtempSync(path.join(tmpdir(), "canary-mock-")),
  symbol = MOCK_SYMBOL,
): Promise<CanaryMockResult> {
  mkdirSync(outDir, { recursive: true });
  const instrumentPath = generateInstrument(outDir, symbol);
  const instrumentId = /^id = (\d+)$/m.exec(readFileSync(instrumentPath, "utf8"))?.[1];
  if (!instrumentId) throw new Error(`${instrumentPath} has no id`);
  const recording = path.join(outDir, "recording.csv");
  const killFile = path.join(outDir, "KILL");
  const ledgerPath = path.join(outDir, "ledger.jsonl");
  const journalPath = path.join(outDir, "broker-journal.jsonl");
  writeFileSync(recording, "");
  let seq = 0;
  const ts = () => String(SESSION_START_NS + BigInt(seq) * 200_000_000n);
  const appendRecord = (): void => {
    seq += 1;
    // SYNTHETIC book around $20: one share fits the $25 per-order cap in config/limits/spy.toml.
    const line =
      seq === 1
        ? `S,${instrumentId},1,${ts()},${ts()},20.00@100;19.99@100,20.01@100;20.02@100`
        : `D,${instrumentId},${seq},B,19.98,${seq},${ts()},${ts()}`;
    appendFileSync(recording, `${line}\n`);
  };

  const bridge = new BridgeClient({
    command: resolveRealBridgeBin(),
    args: [recording, "--follow", "--venue", "external", "--instrument", instrumentPath, "--kill-file", killFile, "--decide-every", "1",
      // The bridge answers nothing else while it waits for records, so the harness ends the feed
      // after 2 s of quiet to read the final status; a live run has no idle stop.
      "--stop-after-idle-ms", "2000"],
    decisionTimeoutMs: 0,
  });
  bridge.start();
  const model = await startMockOpenRouter();
  let placeCalls = 0;
  let cancelCalls = 0;
  const broker = new MockBroker({
    // First order fills in full; the second rests, so only the kill file's halt can end it.
    scenarios: { "1": "filled", "2": "accepted" },
    cash: "10000.00",
    onCall: ({ method }) => {
      if (method === "placeOrder") placeCalls += 1;
      if (method === "cancelOrder") cancelCalls += 1;
    },
  });

  try {
    const external = await createExternalModeGateway({
      bridge,
      broker,
      assetClass: "equity",
      instrument: symbol,
      journalPath,
      reconcileStatusPath: path.join(outDir, "reconcile-status.json"),
      iocCancelAfterMs: 600_000,
      heartbeatIntervalMs: 50,
    });
    external.heartbeat.start();
    const decider = new OpenRouterDecider({
      endpoint: model.endpoint,
      config: { apiKey: "mock-key-not-real", model: "mock/model:free", timeoutMs: 5_000, maxUsdPerDay: 0n, verifiedZeroPrice: true },
    });
    const loop = runLoop({
      bridge,
      decider,
      mode: "live",
      promptVersion: decider.promptVersion,
      model: `openrouter:${decider.config.model}`,
      ledgerPath,
      external: { gateway: external.gateway, assetClass: "equity", reconcile: external.reconcile, reconcileEveryNDecisions: 1_000 },
    }).catch((err: unknown) => err);

    appendRecord(); // decision 1: buy 1 -> filled -> report_execution
    await waitFor("the first order's ledger entry", () => lineCount(ledgerPath) >= 1);
    appendRecord(); // decision 2: buy 1 -> rests at the mock broker
    await waitFor("the second order to rest", async () => ((await bridge.status()).status?.open_orders ?? []).length === 1);

    writeFileSync(killFile, "");
    await waitFor("the heartbeat to cancel the resting order at the mock broker", () => cancelCalls >= 1);
    const loopError = await loop; // ends once the feed goes quiet
    if (loopError instanceof Error) throw loopError;
    const status = (await bridge.status()).status;
    external.heartbeat.stop();
    return {
      ledgerPath,
      journalPath,
      placeCalls,
      cancelCalls,
      filledPosition: status?.position,
      haltedReason: status?.halted ?? undefined,
      openOrdersAfterKill: (status?.open_orders ?? []).length,
      mockModelCalls: model.calls(),
    };
  } finally {
    model.server.closeAllConnections();
    model.server.close();
    await bridge.shutdown();
  }
}

async function main(): Promise<void> {
  const outDir = process.argv[2];
  const r = await runCanaryMock(outDir, process.argv[3]);
  console.log(JSON.stringify(r, null, 2));
  const problems = [
    r.placeCalls === 2 ? "" : `expected 2 mock place calls, got ${r.placeCalls}`,
    r.cancelCalls === 1 ? "" : `expected 1 mock cancel (the kill), got ${r.cancelCalls}`,
    r.haltedReason ? "" : "the bridge never reported halted after the kill file",
    r.openOrdersAfterKill === 0 ? "" : `${r.openOrdersAfterKill} order(s) still open after the kill`,
    r.filledPosition === "1.00000000" ? "" : `bridge position ${r.filledPosition}, expected the reported fill of 1`,
  ].filter(Boolean);
  if (problems.length > 0) throw new Error(`canary mock FAILED: ${problems.join("; ")}`);
  console.log("canary mock passed: decision -> approved order -> mock fill -> report_execution -> ledger/journal; kill file halted and cancelled");
}

if (path.resolve(fileURLToPath(import.meta.url)) === path.resolve(process.argv[1] ?? "")) {
  main().catch((err: unknown) => {
    console.error(err instanceof Error ? err.message : err);
    process.exitCode = 1;
  });
}
