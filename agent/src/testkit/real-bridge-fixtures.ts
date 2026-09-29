// Wave-2 integration (gateway-integration lane): a minimal recording + limits + model, generated
// at runtime, that drives the REAL `qc-bridge` binary through a small, deterministic sequence of
// decisions -- for `just agent-sim-external` and this lane's own real-binary tests, once
// wave2/bridge-control's `--venue external` support has actually landed. Kept out of the repo's
// tracked fixtures (generated fresh into a caller-given temp dir) since nothing else needs these
// exact numbers; the shape mirrors tests/replay/sample_day.csv + tests/replay/limits.toml +
// ml/tests/fixtures/model.json, just deliberately tiny and fully deterministic.
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

/** One snapshot (instrument "1", the v1 hard-coded label used when no `--instrument` flag is
 *  given) plus 13 deltas, none of which ever touch the best bid/ask (so best_bid/best_ask, and
 *  therefore the FakeDecider's buy price, stay constant throughout) -- `decide_every=1` turns
 *  every one of these 14 records into one `next_decision_request` (engine.rs's `ready_to_decide`:
 *  book must be Synced, which the snapshot alone already achieves).
 *
 *  Only the LAST 4 of those 14 decisions carry a non-null `signal`: `qc_inference::features`'s
 *  `FeatureState` needs `OFI_WINDOW` (10) prior book-changing updates before it produces a
 *  feature vector at all (engine/crates/inference/src/features.rs's `FeatureState::update`) --
 *  the snapshot plus the first 9 deltas are pure warm-up (decisions 1-10 are always `no_trade`,
 *  signal `null`), and deltas 10-13 (decisions 11-14) are what actually trade. This is why the
 *  fixture has 14 records, not 4: the first 10 exist only to clear the classifier's warm-up
 *  window, matching a real deployment's own behaviour, not a shortcut around it. Timestamps are
 *  300ms apart, comfortably under `max_order_rate_per_sec` in the limits below. */
const WARMUP_DELTAS = Array.from(
  { length: 9 },
  (_unused, i) => `D,1,${i + 2},B,99.98,5,${1_000_000_000 + (i + 1) * 300_000_000},${1_000_000_000 + (i + 1) * 300_000_000}`,
);
const TRADE_DELTAS = Array.from(
  { length: 4 },
  (_unused, i) => `D,1,${i + 11},B,99.98,5,${1_000_000_000 + (i + 10) * 300_000_000},${1_000_000_000 + (i + 10) * 300_000_000}`,
);
const RECORDING = [
  "S,1,1,1000000000,1000000000,100.00@10;99.99@10,100.02@10;100.03@10",
  ...WARMUP_DELTAS,
  ...TRADE_DELTAS,
  "",
].join("\n");

/** Decision index (1-based, matching `LoopCounts.decisions`) at which the 4 trading decisions
 *  land: the 10 warm-up decisions come first. */
export const FIRST_TRADE_DECISION = 11;

/** Generous on purpose: this fixture exists to exercise protocol v1.2's wire behavior (fill,
 *  partial fill, reject, cancel, reconcile, kill), not to stress the risk gate -- that is
 *  tests/replay/limits.toml's and config/limits/**'s job. Four qty=1 buys at ~100 must all clear
 *  position/notional room, including after the first accepts fully (report_execution having
 *  already applied its fill by the time the next decision is submitted -- see
 *  agent/src/broker/gateway.ts's attemptSubmit, which reports synchronously). */
const LIMITS = `max_position = "100"
max_notional = "100000"
max_order_rate_per_sec = 5
max_daily_loss = "10000"
price_band_bps = 500
stale_data_ms = 60000
max_daily_notional = "1000000"
wash_trade_window_ms = 2000
`;

/** A trivial "always confidently up" qc-linear-v1 model: zero coefficients (features never
 *  matter) and an intercept that softmaxes to essentially 100% "up" regardless of input, so
 *  every decision point in RECORDING above produces the same buy signal -- deterministic, and
 *  independent of this repo's real (trained) classifier fixture. */
function modelJson(): string {
  const model = {
    format: "qc-linear-v1",
    feature_version: "tob-v1",
    features: ["ofi_norm", "queue_imbalance", "microprice_dev_ticks", "spread_ticks"],
    classes: ["down", "flat", "up"],
    coef: [
      [0, 0, 0, 0],
      [0, 0, 0, 0],
      [0, 0, 0, 0],
    ],
    intercept: [-6, -6, 6],
  };
  return `${JSON.stringify(model, null, 1)}\n`;
}

export interface RealBridgeFixtures {
  recordingPath: string;
  limitsPath: string;
  modelPath: string;
  modelSha256: string;
}

/** Writes RECORDING/LIMITS/the model into `dir` (created if needed) and returns their paths plus
 *  the model's own sha256 (computed from the exact bytes written, so it can never drift from
 *  qc-bridge's own `--model-sha256` check). */
export function writeRealBridgeFixtures(dir: string): RealBridgeFixtures {
  mkdirSync(dir, { recursive: true });
  const recordingPath = path.join(dir, "recording.csv");
  const limitsPath = path.join(dir, "limits.toml");
  const modelPath = path.join(dir, "model.json");

  writeFileSync(recordingPath, RECORDING);
  writeFileSync(limitsPath, LIMITS);
  const model = modelJson();
  writeFileSync(modelPath, model);

  return {
    recordingPath,
    limitsPath,
    modelPath,
    modelSha256: createHash("sha256").update(model).digest("hex"),
  };
}
