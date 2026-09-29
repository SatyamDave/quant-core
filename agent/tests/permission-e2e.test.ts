// #32: prove the live decider can only ever place an order one way.
//
// decider-live.test.ts already unit-tests preToolUseHook and buildOptions()'s config in
// isolation. This test goes one level deeper: it runs the live decider's *actual* Options
// object (buildOptions(), unmodified) through a real query() call, with the real Claude Code
// executable swapped for tests/helpers/fakeClaudeCli.mjs (a stub that speaks just enough of the
// SDK's stream-json control protocol to submit tool_use attempts and relay the SDK's real
// PreToolUse-hook decision for each). No network call is made anywhere in this test: the fake
// CLI never talks to Anthropic or any venue, and the only "model output" is the fixed plan of
// tool names below. This proves the denial happens in the SDK's actual permission-decision code
// path (driven by the real preToolUseHook), not merely that our own test calls preToolUseHook
// with the right arguments.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { afterEach, describe, expect, it } from "vitest";
import { buildOptions, NO_TRADE_TOOL, SUBMIT_ORDER_INTENT_TOOL } from "../src/decider/live.js";
import type { Decision } from "../src/types.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const AGENT_ROOT = path.resolve(HERE, "..");
const FAKE_CLI = path.join(HERE, "helpers", "fakeClaudeCli.mjs");

interface PlanEntry {
  tool_name: string;
  tool_input?: Record<string, unknown>;
}

// Every one of these must be denied: two Claude Code built-ins (stripped by `tools: []`, but
// attempted directly here to prove the hook denies them even if something tried to call them
// anyway), two plausible broker MCP tool names (never registered, per ADR-0040 §3),
// and two near-misses of our own wrapper tool's name (a different server, and a suffixed
// variant) — the hook must match the tool name exactly, not by prefix or resemblance.
const DENIED_ATTEMPTS: PlanEntry[] = [
  { tool_name: "Bash", tool_input: { command: "curl https://broker.example.test/mcp/trading" } },
  { tool_name: "WebFetch", tool_input: { url: "https://broker.example.test/mcp/trading" } },
  { tool_name: "mcp__broker__place_equity_order", tool_input: { symbol: "AAPL", side: "buy", qty: 1 } },
  { tool_name: "mcp__broker__place_crypto_order", tool_input: { symbol: "BTC-USD", side: "buy", qty: 1 } },
  { tool_name: "mcp__execution__place_order", tool_input: { symbol: "AAPL", side: "buy", qty: 1 } },
  { tool_name: "mcp__decision__submit_order_intent_v2", tool_input: { request_id: "r", action: "buy", qty: "1", limit_price: "1", rationale: "x" } },
];

const LEGITIMATE_CALL: PlanEntry = {
  tool_name: SUBMIT_ORDER_INTENT_TOOL,
  tool_input: { request_id: "req-legit-1", action: "buy", qty: "1", limit_price: "100.00", rationale: "legit control" },
};

interface RunResult {
  messages: unknown[];
  capture: { outcome?: Decision };
  decisions: Array<{ tool_name: string; decision?: string; reason?: string }>;
}

async function runPlan(plan: PlanEntry[]): Promise<RunResult> {
  const logDir = mkdtempSync(path.join(tmpdir(), "fake-cli-log-"));
  const logPath = path.join(logDir, "decisions.jsonl");
  try {
    const capture: { outcome?: Decision } = {};
    const options = buildOptions({ maxTurns: plan.length + 2, maxBudgetUsd: 1, timeoutMinutes: 1 }, capture);
    options.pathToClaudeCodeExecutable = FAKE_CLI;
    options.executable = "node";
    options.env = { ...process.env, FAKE_CLI_PLAN: JSON.stringify(plan), FAKE_CLI_LOG: logPath };

    const messages: unknown[] = [];
    for await (const message of query({ prompt: "decide", options })) {
      messages.push(message);
    }

    const decisions = readFileSync(logPath, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((line) => JSON.parse(line) as { tool_name: string; decision?: string; reason?: string });
    return { messages, capture, decisions };
  } finally {
    rmSync(logDir, { recursive: true, force: true });
  }
}

function toolResultFor(messages: unknown[], toolUseId: string): { is_error?: boolean; content?: unknown } | undefined {
  for (const msg of messages as Array<{ type?: string; message?: { content?: unknown[] } }>) {
    if (msg.type !== "user") continue;
    const content = msg.message?.content ?? [];
    for (const block of content as Array<Record<string, unknown>>) {
      if (block.type === "tool_result" && block.tool_use_id === toolUseId) {
        return block as { is_error?: boolean; content?: unknown };
      }
    }
  }
  return undefined;
}

describe("live decider end-to-end: every non-wrapper tool request is denied", () => {
  let result: RunResult | undefined;

  afterEach(() => {
    result = undefined;
  });

  it("denies every attack tool name via the real PreToolUse hook, and never runs the wrapper handler for them", async () => {
    result = await runPlan(DENIED_ATTEMPTS);
    const { messages, capture, decisions } = result;

    // tool ids are assigned tu_1..tu_N in plan order by fakeClaudeCli.mjs.
    DENIED_ATTEMPTS.forEach((attempt, i) => {
      const toolUseId = `tu_${i + 1}`;
      const toolResult = toolResultFor(messages, toolUseId);
      expect(toolResult, `no tool_result observed for ${attempt.tool_name}`).toBeDefined();
      expect(toolResult?.is_error, `${attempt.tool_name} should have been denied`).toBe(true);
      expect(String(toolResult?.content)).toMatch(/denied/);
    });

    // The SDK's own recorded permission decision, independent of how fakeClaudeCli.mjs phrased
    // the tool_result: every attempt denied, never "allow" or "no_hook_registered".
    expect(decisions).toHaveLength(DENIED_ATTEMPTS.length);
    for (const d of decisions) {
      expect(d.decision, `${d.tool_name} decision`).toBe("deny");
    }

    // Never reaches the bridge: none of these tool_use attempts ever ran submitOrderIntent's
    // or noTrade's handler, so buildOptions()'s capture object was never populated.
    expect(capture.outcome).toBeUndefined();
  });

  it("still allows the real wrapper tool and runs its handler (proves the hook isn't just always-deny)", async () => {
    result = await runPlan([...DENIED_ATTEMPTS, LEGITIMATE_CALL]);
    const { capture, decisions } = result;

    const attackDecisions = decisions.slice(0, DENIED_ATTEMPTS.length);
    for (const d of attackDecisions) expect(d.decision).toBe("deny");

    const finalDecision = decisions[decisions.length - 1];
    expect(finalDecision?.tool_name).toBe(SUBMIT_ORDER_INTENT_TOOL);
    expect(finalDecision?.decision).toBe("allow");

    expect(capture.outcome).toMatchObject({
      request_id: "req-legit-1",
      action: "buy",
      qty: "1",
      limit_price: "100.00",
    });
  });

  it("no_trade is also allowed by the same hook (both, and only both, wrapper tools pass)", async () => {
    const noTradeCall: PlanEntry = { tool_name: NO_TRADE_TOOL, tool_input: { request_id: "req-legit-2", rationale: "declining" } };
    result = await runPlan([noTradeCall]);
    expect(result.decisions[0]?.decision).toBe("allow");
    expect(result.capture.outcome).toMatchObject({ request_id: "req-legit-2", action: "no_trade" });
  });
});

describe("structural check: no broker address/credentials appear in the agent's own config", () => {
  it("grep of allowedTools in agent/src names only the two wrapper tools", () => {
    // Same check CLAUDE.md documents as the manual review step; running it in CI makes it a
    // check that runs, not one that only holds "as of the last time someone looked."
    const output = execFileSync("grep", ["-rn", "allowedTools", path.join(AGENT_ROOT, "src")], { encoding: "utf8" });
    const lines = output.split("\n").filter(Boolean);
    expect(lines.length).toBeGreaterThan(0);
    for (const line of lines) {
      // Only the matched text, not the file path: a checkout under a directory named after a
      // broker would otherwise fail this for a reason unrelated to the config.
      expect(line.replace(/^[^:]+:\d+:/, "")).not.toMatch(/broker/i);
    }
  });

  it("no broker MCP endpoint or tool name is present anywhere in agent/src", () => {
    let output = "";
    try {
      output = execFileSync("grep", ["-rniE", "mcp__broker|broker\\.example", path.join(AGENT_ROOT, "src")], { encoding: "utf8" });
    } catch (err) {
      // grep exits 1 when it finds nothing; that's the pass case, so only real command
      // failures (exit code >1, or no stdout at all) should surface as a test failure.
      const asError = err as { status?: number; stdout?: string };
      if (asError.status !== 1) throw err;
      output = asError.stdout ?? "";
    }
    // A code comment explaining *why* the endpoint must never appear (this file's own docblock,
    // ADR references, decider/live.ts's header) is fine; an actual mcpServers config entry
    // pointing at a broker is not. Distinguish by requiring every hit to be a `//` comment line.
    const nonCommentHits = output
      .split("\n")
      .filter(Boolean)
      .filter((line) => !/:\s*(\/\/|\*|\/\*)/.test(line.replace(/^[^:]+:\d+:/, "")));
    expect(nonCommentHits).toEqual([]);
  });
});
