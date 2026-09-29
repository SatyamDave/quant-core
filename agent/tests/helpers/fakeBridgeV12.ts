import { fileURLToPath } from "node:url";
import path from "node:path";
import { BridgeClient, type BridgeClientOptions } from "../../src/bridge.js";
import { AGENT_ROOT, TSX_BIN } from "./fakeBridge.js";

const FAKE_BRIDGE_V12_SCRIPT = path.join(AGENT_ROOT, "src", "testkit", "fake-bridge-v12.ts");

export interface StartFakeBridgeV12Options {
  venue?: "sim" | "external";
  scenario?: string;
  mode?: string;
  timeoutMs?: number;
}

/** Spawns src/testkit/fake-bridge-v12.ts as a real child process via tsx. */
export function startFakeBridgeV12(opts: StartFakeBridgeV12Options = {}): BridgeClient {
  const args = [FAKE_BRIDGE_V12_SCRIPT, "--venue", opts.venue ?? "external"];
  if (opts.scenario) args.push("--scenario", opts.scenario);
  const clientOpts: BridgeClientOptions = {
    command: TSX_BIN,
    args,
    env: { ...process.env, ...(opts.mode ? { QC_FAKE_BRIDGE_V12_MODE: opts.mode } : {}) },
    timeoutMs: opts.timeoutMs,
  };
  const client = new BridgeClient(clientOpts);
  client.start();
  return client;
}

export const V12_SCENARIO_FIXTURE = fileURLToPath(new URL("../fixtures/scenario.json", import.meta.url));
