// Unit tests only: this file never calls query() / the Anthropic API. It checks the *config*
// (the Options object buildOptions produces) and the PreToolUse hook function in isolation.
import type { HookJSONOutput, PreToolUseHookInput, SyncHookJSONOutput } from "@anthropic-ai/claude-agent-sdk";
import { describe, expect, it } from "vitest";
import {
  ALLOWED_LIVE_TOOLS,
  buildOptions,
  listPromptVersions,
  LiveDecider,
  NO_TRADE_TOOL,
  preToolUseHook,
  PROMPT_VERSION,
  resolveLivePolicy,
  resolvePromptVersion,
  SDK_SERVER_NAME,
  SUBMIT_ORDER_INTENT_TOOL,
} from "../src/decider/live.js";

describe("resolvePromptVersion (issue #54: QC_PROMPT_VERSION override for promote.py A/B tests)", () => {
  it("defaults to the committed PROMPT_VERSION when unset", () => {
    expect(resolvePromptVersion({})).toBe(PROMPT_VERSION);
  });

  it("accepts a known, on-disk version", () => {
    expect(resolvePromptVersion({ QC_PROMPT_VERSION: "v1" })).toBe("v1");
  });

  it("refuses an unknown version rather than guessing (fail closed)", () => {
    expect(() => resolvePromptVersion({ QC_PROMPT_VERSION: "does-not-exist" })).toThrow(
      /not a known prompt version/,
    );
  });

  it("listPromptVersions includes the committed default", () => {
    expect(listPromptVersions()).toContain(PROMPT_VERSION);
  });
});

describe("LiveDecider construction (issue #54: ensemble members pin a prompt version)", () => {
  it("accepts an explicit, known promptVersion override", () => {
    expect(new LiveDecider({ promptVersion: "v1" }).promptVersion).toBe("v1");
  });

  it("refuses an explicit, unknown promptVersion override just as it would from the env", () => {
    expect(() => new LiveDecider({ promptVersion: "does-not-exist" })).toThrow(/not a known prompt version/);
  });
});

describe("resolveLivePolicy", () => {
  it("refuses to start when ANTHROPIC_API_KEY is not set", () => {
    expect(() => resolveLivePolicy({})).toThrow(/ANTHROPIC_API_KEY is not set/);
  });

  it("refuses to start when ai_gate.py denies loop-agent-eval (POLICY.yaml default: disabled)", () => {
    // Real repo state: autonomy/POLICY.yaml has loop-agent-eval enabled:false, daily_usd:0,
    // max_turns:0, timeout_minutes:0. This must deny until a human changes those numbers.
    expect(() =>
      resolveLivePolicy({ ...process.env, ANTHROPIC_API_KEY: "sk-test-not-real" }),
    ).toThrow(/ai_gate\.py denied loop-agent-eval/);
  });
});

describe("buildOptions", () => {
  const policy = { maxTurns: 2, maxBudgetUsd: 0.5, timeoutMinutes: 1 };

  it("registers exactly one MCP server exposing exactly the two wrapper tools", () => {
    const options = buildOptions(policy, {});
    expect(Object.keys(options.mcpServers ?? {})).toEqual([SDK_SERVER_NAME]);
    expect(options.allowedTools).toEqual([SUBMIT_ORDER_INTENT_TOOL, NO_TRADE_TOOL]);
  });

  it("strips every Claude Code built-in tool", () => {
    const options = buildOptions(policy, {});
    expect(options.tools).toEqual([]);
  });

  it("uses dontAsk permission mode with no human approver", () => {
    const options = buildOptions(policy, {});
    expect(options.permissionMode).toBe("dontAsk");
  });

  it("installs the PreToolUse hard-deny hook", () => {
    const options = buildOptions(policy, {});
    expect(options.hooks?.PreToolUse).toHaveLength(1);
    expect(options.hooks?.PreToolUse?.[0]?.hooks).toContain(preToolUseHook);
  });

  it("passes maxTurns and maxBudgetUsd through from policy", () => {
    const options = buildOptions(policy, {});
    expect(options.maxTurns).toBe(2);
    expect(options.maxBudgetUsd).toBe(0.5);
  });

  it("sets outputFormat to the decision schema", () => {
    const options = buildOptions(policy, {});
    expect(options.outputFormat).toMatchObject({ type: "json_schema" });
    expect((options.outputFormat as { schema: { title?: string } }).schema.title).toBe("Decision");
  });
});

describe("preToolUseHook", () => {
  const signal = new AbortController().signal;

  // preToolUseHook only ever returns the synchronous shape (never `{ async: true }`); this
  // narrows HookJSONOutput's union so tests can read `hookSpecificOutput` directly.
  function permissionDecisionOf(out: HookJSONOutput): string | undefined {
    if ("async" in out && out.async) throw new Error("preToolUseHook unexpectedly returned an async hook output");
    const sync = out as SyncHookJSONOutput;
    return sync.hookSpecificOutput && "permissionDecision" in sync.hookSpecificOutput
      ? sync.hookSpecificOutput.permissionDecision
      : undefined;
  }

  function preToolUseInput(tool_name: string): PreToolUseHookInput {
    return {
      hook_event_name: "PreToolUse",
      tool_name,
      tool_input: {},
      tool_use_id: "tool-use-1",
      session_id: "session-1",
      transcript_path: "/tmp/transcript.jsonl",
      cwd: "/tmp",
    };
  }

  it("allows the submit_order_intent wrapper tool", async () => {
    const out = await preToolUseHook(preToolUseInput(SUBMIT_ORDER_INTENT_TOOL), "tool-use-1", { signal });
    expect(permissionDecisionOf(out)).toBe("allow");
  });

  it("allows the no_trade wrapper tool", async () => {
    const out = await preToolUseHook(preToolUseInput(NO_TRADE_TOOL), "tool-use-1", { signal });
    expect(permissionDecisionOf(out)).toBe("allow");
  });

  it("denies a Claude Code built-in tool name", async () => {
    const out = await preToolUseHook(preToolUseInput("Bash"), "tool-use-1", { signal });
    expect(permissionDecisionOf(out)).toBe("deny");
  });

  it("denies an unrelated MCP tool name, including one that merely resembles the allowed ones", async () => {
    const out = await preToolUseHook(preToolUseInput("mcp__execution__place_order"), "tool-use-1", { signal });
    expect(permissionDecisionOf(out)).toBe("deny");
  });

  it("denies every tool name that isn't exactly one of the two allowed tools", async () => {
    for (const name of ["Read", "Write", "WebFetch", "mcp__decision__submit_order_intent_v2"]) {
      const out = await preToolUseHook(preToolUseInput(name), "tool-use-1", { signal });
      expect(permissionDecisionOf(out)).toBe("deny");
    }
  });

  it("ignores non-PreToolUse hook events", async () => {
    const out = await preToolUseHook(
      { hook_event_name: "SessionEnd", session_id: "s", transcript_path: "/t", cwd: "/", reason: "clear" } as never,
      undefined,
      { signal },
    );
    expect(out).toEqual({});
  });
});

it("ALLOWED_LIVE_TOOLS is exactly the two wrapper tools, nothing more", () => {
  expect(ALLOWED_LIVE_TOOLS).toEqual([SUBMIT_ORDER_INTENT_TOOL, NO_TRADE_TOOL]);
});
