// The wave2-spec "prove it" requirement for #46: `just record-quotes --dry-run` must produce a
// file qc-replay can actually replay. This runs the real recorder CLI (as a child process, exactly
// the way `just record-quotes` will invoke it) against the mock broker, then feeds the
// resulting CSV to the real qc-replay binary and asserts it exits 0 -- not a claim, a build and a
// subprocess run.
//
// Two runs (wave3-spec lane 5 / issue #43/#46 follow-up): equity, where pollBook() gets real
// multi-level depth from the mock broker's getBook (proving qc_orderbook's apply_snapshot accepts a
// multi-level, sorted, non-crossed snapshot -- not just a single top-of-book line); and crypto,
// where pollBook() has no L2 book to read and falls back to a single top-of-book level, proving
// that path still produces a file qc-replay accepts too.
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { repoRoot } from "../../src/paths.js";

const ROOT = repoRoot();
const AGENT_DIR = path.join(ROOT, "agent");
const ENGINE_DIR = path.join(ROOT, "engine");
const TSX_BIN = path.join(AGENT_DIR, "node_modules", ".bin", "tsx");
const CLI = path.join(AGENT_DIR, "src", "recorder", "cli.ts");
const QC_REPLAY_BIN = path.join(ENGINE_DIR, "target", "debug", "qc-replay");
const LIMITS = path.join(ROOT, "tests", "replay", "limits.toml");

function ensureQcReplayBuilt(): void {
  if (existsSync(QC_REPLAY_BIN)) return;
  // Same crate `just replay`/`just agent-sim` build; debug (not --release) is plenty for this
  // proof and builds in a few seconds.
  execFileSync("cargo", ["build", "-q", "-p", "qc-replay"], { cwd: ENGINE_DIR, stdio: "inherit" });
}

describe("just record-quotes --dry-run output replays through qc-replay", () => {
  let dir: string | undefined;
  afterEach(() => {
    if (dir) rmSync(dir, { recursive: true, force: true });
    dir = undefined;
  });

  it("runs the CLI dry-run against the mock broker, then qc-replay accepts the recorded file", () => {
    ensureQcReplayBuilt();
    dir = mkdtempSync(path.join(tmpdir(), "recorder-replay-"));
    const csvPath = path.join(dir, "recorded.csv");
    const catalogDir = path.join(dir, "catalog");
    const orderLog = path.join(dir, "orders.log");

    const cliOutput = execFileSync(
      TSX_BIN,
      [
        CLI,
        "--dry-run",
        "--instrument-id",
        "1", // matches qc_replay's SAMPLE_INSTRUMENT and tests/replay/limits.toml
        "--symbol",
        "SPY",
        // 50 ms polls with a 1 s gap allowance: a real gap check, but with room for a slow CI
        // runner's scheduling jitter (5 ms polls with a 25 ms allowance flagged gaps on CI).
        "--poll-interval-ms",
        "50",
        "--gap-multiple",
        "20",
        "--request-budget",
        "10",
        "--out",
        csvPath,
        "--catalog-dir",
        catalogDir,
        "--dataset",
        "recorder-replay-proof-test",
      ],
      { cwd: AGENT_DIR, encoding: "utf8" },
    );
    expect(cliOutput).toContain("wrote 10 record(s)");

    expect(existsSync(csvPath)).toBe(true);
    const csv = readFileSync(csvPath, "utf8");
    const dataLines = csv.split("\n").filter((l) => l && !l.startsWith("#"));
    expect(dataLines).toHaveLength(10);
    expect(dataLines.every((l) => l.startsWith("S,1,"))).toBe(true);
    // The point of this run: real depth, not just the best price -- each side carries more than
    // one ";"-separated level (the mock's synthetic 3-level book).
    expect(dataLines.every((l) => l.split(",")[5]!.includes(";") && l.split(",")[6]!.includes(";"))).toBe(true);

    const manifestPath = path.join(catalogDir, "recorder-replay-proof-test.json");
    expect(existsSync(manifestPath)).toBe(true);
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as {
      quality: { ok: boolean };
      depth: { l2: number; top_of_book: number };
      files: Record<string, string>;
    };
    expect(manifest.quality.ok).toBe(true);
    expect(manifest.depth).toEqual({ l2: 10, top_of_book: 0 });
    expect(Object.keys(manifest.files)).toHaveLength(1);

    // The actual proof: hand the recorded file to the real qc-replay binary.
    const replayOutput = execFileSync(QC_REPLAY_BIN, [csvPath, orderLog, LIMITS], { encoding: "utf8" });
    expect(replayOutput).toMatch(/^replayed .*: \d+ lines, \d+ submits, \d+ risk rejects, \d+ halts/);
    expect(existsSync(orderLog)).toBe(true);
  }, 60_000);

  it("falls back to a flagged top-of-book recording for crypto (no L2 book), and qc-replay still accepts it", () => {
    ensureQcReplayBuilt();
    dir = mkdtempSync(path.join(tmpdir(), "recorder-replay-crypto-"));
    const csvPath = path.join(dir, "recorded.csv");
    const catalogDir = path.join(dir, "catalog");
    const orderLog = path.join(dir, "orders.log");

    const cliOutput = execFileSync(
      TSX_BIN,
      [
        CLI,
        "--dry-run",
        "--instrument-id",
        "1",
        "--symbol",
        "BTC-USD",
        "--asset-class",
        "crypto",
        "--poll-interval-ms",
        "50",
        "--gap-multiple",
        "20",
        "--request-budget",
        "10",
        "--out",
        csvPath,
        "--catalog-dir",
        catalogDir,
        "--dataset",
        "recorder-replay-proof-test-crypto",
      ],
      { cwd: AGENT_DIR, encoding: "utf8" },
    );
    expect(cliOutput).toContain("wrote 10 record(s)");
    expect(cliOutput).toContain("depth: l2=0 top_of_book=10");

    const csv = readFileSync(csvPath, "utf8");
    const dataLines = csv.split("\n").filter((l) => l && !l.startsWith("#"));
    expect(dataLines).toHaveLength(10);
    // Single level per side -- no ";" -- the explicit top-of-book fallback, not fabricated depth.
    expect(dataLines.every((l) => !l.split(",")[5]!.includes(";") && !l.split(",")[6]!.includes(";"))).toBe(true);

    const manifestPath = path.join(catalogDir, "recorder-replay-proof-test-crypto.json");
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as { depth: { l2: number; top_of_book: number } };
    expect(manifest.depth).toEqual({ l2: 0, top_of_book: 10 });

    const replayOutput = execFileSync(QC_REPLAY_BIN, [csvPath, orderLog, LIMITS], { encoding: "utf8" });
    expect(replayOutput).toMatch(/^replayed .*: \d+ lines, \d+ submits, \d+ risk rejects, \d+ halts/);
  }, 60_000);
});
