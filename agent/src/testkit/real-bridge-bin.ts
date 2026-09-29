// Resolves the real `qc-bridge` binary for the wave-2 integration tests/demo, building it if
// necessary -- same convention as justfile's `agent-sim` recipe ("a build failure fails the
// recipe: a silent fallback to the fake bridge would hide it"). QC_BRIDGE_BIN overrides this
// (e.g. to point at a prebuilt binary in CI, or deliberately at the fake for a one-off check).
import { existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { repoRoot } from "../paths.js";

export function resolveRealBridgeBin(): string {
  if (process.env.QC_BRIDGE_BIN) return process.env.QC_BRIDGE_BIN;
  const engineDir = path.join(repoRoot(), "engine");
  const bin = path.join(engineDir, "target", "release", "qc-bridge");
  // Always ask cargo, every call: it is a fast no-op when nothing changed, and unlike an
  // existsSync shortcut it can never hand back a binary built from stale source.
  execFileSync("cargo", ["build", "--release", "--locked", "-p", "qc-bridge"], {
    cwd: engineDir,
    stdio: "inherit",
  });
  if (!existsSync(bin)) {
    throw new Error(`cargo build succeeded but ${bin} is still missing`);
  }
  return bin;
}
