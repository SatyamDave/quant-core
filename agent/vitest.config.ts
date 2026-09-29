import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    testTimeout: 10_000,
    // Many test files spawn real qc-bridge processes and time the kill switch; running files in
    // parallel caused an occasional SIGKILL of a bridge child under load (seen locally on #87).
    // One file at a time keeps those timing tests trustworthy.
    fileParallelism: false,
  },
});
