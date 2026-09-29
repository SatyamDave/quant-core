import { fileURLToPath } from "node:url";
import path from "node:path";
import { BridgeClient, type BridgeClientOptions } from "../../src/bridge.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const AGENT_ROOT = path.resolve(HERE, "..", "..");
export const TSX_BIN = path.join(AGENT_ROOT, "node_modules", ".bin", "tsx");
export const FAKE_BRIDGE_SCRIPT = path.join(AGENT_ROOT, "src", "testkit", "fake-bridge.ts");
export const SCENARIO_FIXTURE = path.join(AGENT_ROOT, "tests", "fixtures", "scenario.json");

export interface StartFakeBridgeOptions {
  mode?: string;
  scenario?: string;
  timeoutMs?: number;
}

/** Spawns tests/../src/testkit/fake-bridge.ts as a real child process via tsx. */
export function startFakeBridge(opts: StartFakeBridgeOptions = {}): BridgeClient {
  const args = [FAKE_BRIDGE_SCRIPT];
  if (opts.scenario) args.push("--scenario", opts.scenario);
  const clientOpts: BridgeClientOptions = {
    command: TSX_BIN,
    args,
    env: { ...process.env, ...(opts.mode ? { QC_FAKE_BRIDGE_MODE: opts.mode } : {}) },
    timeoutMs: opts.timeoutMs,
  };
  const client = new BridgeClient(clientOpts);
  client.start();
  return client;
}
