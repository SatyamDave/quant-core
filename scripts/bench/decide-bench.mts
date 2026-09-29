// Latency bench harness for issues #31/#55 (wave2/latency). Drives the REAL qc-bridge binary
// through the REAL agent-side BridgeClient/Decider/runLoop code (imported unmodified from
// agent/src/ -- this file never edits agent/src or engine/crates/bridge, per the wave-2 lane C
// rule) so the measured numbers reflect production code, not a re-implementation of it.
//
// Wraps BridgeClient and Decider in thin timing proxies and calls the unmodified `runLoop` so
// "full loop" is the exact production sequence (next_decision_request -> decide ->
// submit_order_intent/no_trade -> ledger append), with per-decision timings for three
// components recorded alongside it: bridge_request_ms (the next_decision_request round trip
// through the TS BridgeClient), decide_ms (the decider's own decide() call, no IPC), and
// bridge_submit_ms (the submit_order_intent/no_trade round trip). total_ms is their per-decision
// sum -- the "full loop" number -- computed after the run so it reflects exactly what happened
// on that iteration, not a separately-timed re-run.
//
// Two modes:
//   --mode fake    FakeDecider (no network); with --capture-dir, also writes each decision as a
//                   decision_ledger_entry-shaped fixture, for the replay pass below.
//   --mode replay  ReplayDecider loaded from --fixtures-dir. Must run against a bridge started
//                   with the IDENTICAL recording/limits/model/--decide-every as the fake-mode
//                   capture pass: the bridge is deterministic (root CLAUDE.md rule 7), so the
//                   same DecisionRequest sequence recurs byte-for-byte, which is what
//                   ReplayDecider's exact-match check requires.
//
// One JSON object per decision on stdout; scripts/bench/latency.py parses it and computes
// percentiles. Human-readable progress goes to stderr only, so stdout stays parseable NDJSON.
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import { BridgeClient } from "../../agent/src/bridge.js";
import { FakeDecider } from "../../agent/src/decider/fake.js";
import { loadReplayFixtures, ReplayDecider } from "../../agent/src/decider/replay.js";
import type { Decider } from "../../agent/src/decider/types.js";
import { runLoop } from "../../agent/src/loop.js";
import { validateOrThrow } from "../../agent/src/schema.js";
import type { BridgeResponse, Decision, DecisionLedgerEntry, DecisionRequest, LedgerMode, OrderIntent } from "../../agent/src/types.js";

interface Args {
  mode: "fake" | "replay";
  n: number;
  bridgeBin: string;
  bridgeArgs: string[];
  probThreshold?: number;
  fixturesDir?: string;
  captureDir?: string;
  ledgerPath: string;
}

function parseArgs(argv: string[]): Args {
  const get = (flag: string): string | undefined => {
    const i = argv.indexOf(flag);
    return i === -1 ? undefined : argv[i + 1];
  };
  const mode = get("--mode");
  if (mode !== "fake" && mode !== "replay") throw new Error(`--mode must be fake|replay, got ${mode}`);
  const n = Number(get("--n") ?? "0");
  if (!(n > 0)) throw new Error("--n must be a positive integer");
  const bridgeBin = get("--bridge-bin");
  if (!bridgeBin) throw new Error("--bridge-bin is required");
  const bridgeArgs = (get("--bridge-args") ?? "").split(" ").filter(Boolean);
  const ledgerPath = get("--ledger-path");
  if (!ledgerPath) throw new Error("--ledger-path is required");
  const probThresholdRaw = get("--prob-threshold");
  return {
    mode,
    n,
    bridgeBin,
    bridgeArgs,
    probThreshold: probThresholdRaw === undefined ? undefined : Number(probThresholdRaw),
    fixturesDir: get("--fixtures-dir"),
    captureDir: get("--capture-dir"),
    ledgerPath,
  };
}

/** Times next_decision_request and submit_order_intent/no_trade; forces the loop to stop after
 *  exactly `limit` decisions by faking an exhausted feed rather than editing loop.ts to accept a
 *  count. Real exhaustion (the recording running out first) still ends the loop normally. */
class TimingBridge {
  readonly requestMs: number[] = [];
  readonly submitMs: number[] = [];
  private served = 0;

  constructor(
    private readonly inner: BridgeClient,
    private readonly limit: number,
  ) {}

  async nextDecisionRequest(): Promise<BridgeResponse> {
    if (this.served >= this.limit) {
      return { v: 1, id: "bench-stop", ok: true, decision_request: null };
    }
    const t0 = performance.now();
    const r = await this.inner.nextDecisionRequest();
    this.requestMs.push(performance.now() - t0);
    if (r.ok && r.decision_request) this.served += 1;
    return r;
  }

  async submitOrderIntent(intent: OrderIntent): Promise<BridgeResponse> {
    const t0 = performance.now();
    const r = await this.inner.submitOrderIntent(intent);
    this.submitMs.push(performance.now() - t0);
    return r;
  }

  async noTrade(requestId: string, reason: string): Promise<BridgeResponse> {
    const t0 = performance.now();
    const r = await this.inner.noTrade(requestId, reason);
    this.submitMs.push(performance.now() - t0);
    return r;
  }

  async shutdown(): Promise<void> {
    return this.inner.shutdown();
  }
}

/** Times decide() and, in capture mode, records a decision_ledger_entry-shaped fixture per
 *  decision (request + decision only -- the fields ReplayDecider actually needs to match on and
 *  replay; `result` is filled in after submit by the real loop, which this fixture does not need
 *  since ReplayDecider never reads it). */
class TimingDecider {
  readonly decideMs: number[] = [];
  private captured = 0;

  constructor(
    private readonly inner: Decider,
    private readonly captureDir?: string,
  ) {}

  async decide(req: DecisionRequest) {
    const t0 = performance.now();
    const out = await this.inner.decide(req);
    this.decideMs.push(performance.now() - t0);
    if (this.captureDir) this.capture(req, out.decision);
    return out;
  }

  private capture(request: DecisionRequest, decision: Decision): void {
    const entry: DecisionLedgerEntry = {
      request,
      decision,
      // ReplayDecider only reads `request` (to match) and `decision` (to replay); `result` isn't
      // known yet at decide-time (submit happens after), so this is a placeholder, never read.
      result: null,
      mode: "fake",
      prompt_version: "bench-v1",
      model: "fake-rule-v1",
    };
    validateOrThrow("decision_ledger_entry", entry);
    const name = `d${String(this.captured).padStart(6, "0")}.json`;
    writeFileSync(path.join(this.captureDir!, name), JSON.stringify(entry));
    this.captured += 1;
  }
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));

  if (args.mode === "fake" && !args.captureDir) throw new Error("--mode fake needs --capture-dir");
  if (args.mode === "replay" && !args.fixturesDir) throw new Error("--mode replay needs --fixtures-dir");
  if (args.captureDir) mkdirSync(args.captureDir, { recursive: true });

  const realBridge = new BridgeClient({ command: args.bridgeBin, args: args.bridgeArgs });
  realBridge.start();
  const bridge = new TimingBridge(realBridge, args.n);

  const innerDecider: Decider =
    args.mode === "fake" ? new FakeDecider({ probThreshold: args.probThreshold }) : new ReplayDecider(loadReplayFixtures(args.fixturesDir!));
  const decider = new TimingDecider(innerDecider, args.mode === "fake" ? args.captureDir : undefined);

  const ledgerMode: LedgerMode = args.mode;
  let counts;
  try {
    // Cast: TimingBridge/TimingDecider are structural stand-ins for BridgeClient/Decider (tsx
    // does not type-check at runtime); runLoop only calls the methods both wrappers implement.
    counts = await runLoop({
      bridge: bridge as unknown as BridgeClient,
      decider: decider as unknown as Decider,
      mode: ledgerMode,
      promptVersion: "bench-v1",
      model: args.mode === "fake" ? "fake-rule-v1" : "replay-fixture",
      ledgerPath: args.ledgerPath,
    });
  } finally {
    await bridge.shutdown();
  }

  const n = Math.min(bridge.requestMs.length, decider.decideMs.length, bridge.submitMs.length);
  if (n < args.n) {
    throw new Error(
      `only ${n} decisions completed before the feed exhausted; need --n ${args.n}. ` +
        "Lower --decide-every, use a longer recording, or lower --n.",
    );
  }
  for (let i = 0; i < n; i++) {
    const bridgeRequestMs = bridge.requestMs[i]!;
    const decideMs = decider.decideMs[i]!;
    const bridgeSubmitMs = bridge.submitMs[i]!;
    process.stdout.write(
      `${JSON.stringify({
        mode: args.mode,
        i,
        bridge_request_ms: bridgeRequestMs,
        decide_ms: decideMs,
        bridge_submit_ms: bridgeSubmitMs,
        total_ms: bridgeRequestMs + decideMs + bridgeSubmitMs,
      })}\n`,
    );
  }
  process.stderr.write(
    `${args.mode}: ${n} decisions (accepted=${counts.accepted} rejected=${counts.rejected} no_trade=${counts.noTrades})\n`,
  );
}

main().catch((err: unknown) => {
  process.stderr.write(`${err instanceof Error ? err.stack ?? err.message : String(err)}\n`);
  process.exitCode = 1;
});
