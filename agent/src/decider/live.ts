// Agent SDK decider (QC_AGENT_MODE=live). The model's only path to a decision is one of two
// in-process wrapper tools; every other tool is unavailable (`tools: []`, only this one MCP
// server registered) and, as a final code-level gate independent of that config, a PreToolUse
// hook denies any tool_name that isn't exactly one of the two. The model never sees a broker
// adapter, a broker credential, or a real submit path — this decider only produces a
// Decision; src/loop.ts is what calls the real qc-bridge afterwards.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  createSdkMcpServer,
  type HookCallback,
  type Options,
  query,
  tool,
} from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";
import { repoRoot } from "../paths.js";
import { loadRawSchema } from "../schema.js";
import { usdToDecimalString } from "../decimal.js";
import type { DecimalString, Decision, DecisionRequest } from "../types.js";
import type { Decider, DecisionOutcome } from "./types.js";

export const PROMPT_VERSION = "v1";
export const SDK_SERVER_NAME = "decision";
export const SUBMIT_ORDER_INTENT_TOOL = `mcp__${SDK_SERVER_NAME}__submit_order_intent`;
export const NO_TRADE_TOOL = `mcp__${SDK_SERVER_NAME}__no_trade`;
export const ALLOWED_LIVE_TOOLS = [SUBMIT_ORDER_INTENT_TOOL, NO_TRADE_TOOL] as const;

const DECIMAL_STRING = /^-?[0-9]+(\.[0-9]{1,8})?$/;

const submitOrderIntentShape = {
  request_id: z.string(),
  action: z.enum(["buy", "sell"]),
  qty: z.string().regex(DECIMAL_STRING),
  limit_price: z.string().regex(DECIMAL_STRING),
  confidence: z.number().min(0).max(1).optional(),
  rationale: z.string().max(500),
};

const noTradeShape = {
  request_id: z.string(),
  rationale: z.string().max(500),
};

/**
 * Authoritative gate: fires on every tool call (no matcher) and denies anything whose name
 * isn't exactly one of the two wrapper tools, independent of `allowedTools`/`permissionMode`.
 * A permission rule that never runs is not a permission that held — this hook always runs.
 */
export const preToolUseHook: HookCallback = async (input) => {
  if (input.hook_event_name !== "PreToolUse") return {};
  const allowed = (ALLOWED_LIVE_TOOLS as readonly string[]).includes(input.tool_name);
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: allowed ? "allow" : "deny",
      permissionDecisionReason: allowed
        ? "wrapper decision tool"
        : `denied: only ${ALLOWED_LIVE_TOOLS.join(", ")} may run in the live decider`,
    },
  };
};

const PROMPT_FILE_PATTERN = /^decide-(.+)\.md$/;

function promptsDir(): string {
  return path.join(repoRoot(), "agent", "src", "prompts");
}

function promptPath(version: string): string {
  return path.join(promptsDir(), `decide-${version}.md`);
}

/** Every prompt version actually present on disk (`decide-<v>.md`), sorted. This is the
 *  allowlist `resolvePromptVersion` validates QC_PROMPT_VERSION against -- there is no separate,
 *  hand-maintained list to drift out of sync with the files themselves. */
export function listPromptVersions(): string[] {
  return readdirSync(promptsDir())
    .map((name) => PROMPT_FILE_PATTERN.exec(name)?.[1])
    .filter((v): v is string => v !== undefined)
    .sort();
}

/**
 * Issue #54's prompt-version override (`QC_PROMPT_VERSION`), so `scripts/eval/promote.py` can A/B
 * a prompt change as a challenger without editing the committed default. Fails closed: an unset
 * env var keeps today's committed `PROMPT_VERSION`; a set-but-unrecognized one refuses to start
 * rather than silently falling back (root CLAUDE.md rule 12 -- "a check that could not run is not
 * a check that passed" applies just as much to "a version that doesn't exist is not a version").
 */
function requireKnownPromptVersion(version: string, source: string): string {
  const available = listPromptVersions();
  if (!available.includes(version)) {
    throw new Error(
      `${source}=${JSON.stringify(version)} is not a known prompt version ` +
        `(available: ${available.join(", ") || "(none found)"}); refusing to guess.`,
    );
  }
  return version;
}

export function resolvePromptVersion(env: NodeJS.ProcessEnv = process.env): string {
  const requested = env.QC_PROMPT_VERSION;
  return requested === undefined ? PROMPT_VERSION : requireKnownPromptVersion(requested, "QC_PROMPT_VERSION");
}

export function buildPrompt(req: DecisionRequest, promptVersion: string = PROMPT_VERSION): string {
  const template = readFileSync(promptPath(promptVersion), "utf8");
  return `${template}\n\n## Decision request\n\n\`\`\`json\n${JSON.stringify(req, null, 2)}\n\`\`\`\n`;
}

export interface LivePolicy {
  maxTurns: number;
  maxBudgetUsd: number;
  timeoutMinutes: number;
  model?: string;
}

/** Runs `ai_gate.py <loop>` and returns its GITHUB_OUTPUT key=value lines; throws when it denies.
 *  Shared by every decider that spends model calls (live, openrouter). */
export function runAiGate(loop: string, mode: string, env: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const outDir = mkdtempSync(path.join(tmpdir(), "ai-gate-"));
  const outFile = path.join(outDir, "output");
  const result = spawnSync("python3", ["scripts/ci/ai_gate.py", loop], {
    cwd: repoRoot(),
    env: { ...env, GITHUB_OUTPUT: outFile },
    encoding: "utf8",
  });
  if (result.status !== 0) {
    const reason = (result.stdout ?? result.stderr ?? "").trim();
    throw new Error(`${mode} mode refused: ai_gate.py denied ${loop}: ${reason}`);
  }
  return Object.fromEntries(
    readFileSync(outFile, "utf8")
      .split("\n")
      .filter((line) => line.includes("="))
      .map((line) => {
        const idx = line.indexOf("=");
        return [line.slice(0, idx), line.slice(idx + 1)];
      }),
  );
}

/**
 * Fail closed: live mode may run only with an API key present AND `ai_gate.py loop-agent-eval`
 * allowing it (autonomy/POLICY.yaml, disabled with a zero budget until a human sets real
 * numbers). Reuses ai_gate.py's own GITHUB_OUTPUT contract for the budget numbers rather than
 * re-parsing POLICY.yaml's YAML-with-comments format in TypeScript — one parser, one place.
 */
export function resolveLivePolicy(env: NodeJS.ProcessEnv = process.env): LivePolicy {
  if (!env.ANTHROPIC_API_KEY) {
    throw new Error("live mode refused: ANTHROPIC_API_KEY is not set");
  }
  const output = runAiGate("loop-agent-eval", "live", env);
  const maxTurns = Number(output.max_turns);
  const maxBudgetUsd = Number(output.max_budget_usd);
  const timeoutMinutes = Number(output.timeout_minutes);
  if (!Number.isFinite(maxTurns) || !Number.isFinite(maxBudgetUsd) || !Number.isFinite(timeoutMinutes)) {
    throw new Error(`live mode refused: ai_gate.py allowed but gave no usable budget (${JSON.stringify(output)})`);
  }
  return { maxTurns, maxBudgetUsd, timeoutMinutes };
}

export function buildOptions(policy: LivePolicy, capture: { outcome?: Decision }): Options {
  const submitOrderIntent = tool(
    "submit_order_intent",
    "Submit the final buy/sell decision for this request. The engine independently re-checks " +
      "risk; this call only records the proposed decision, it does not place an order.",
    submitOrderIntentShape,
    async (args) => {
      capture.outcome = {
        request_id: args.request_id,
        action: args.action,
        qty: args.qty,
        limit_price: args.limit_price,
        confidence: args.confidence,
        rationale: args.rationale,
      };
      return { content: [{ type: "text", text: "recorded" }] };
    },
  );
  const noTrade = tool("no_trade", "Decline to trade this request.", noTradeShape, async (args) => {
    capture.outcome = { request_id: args.request_id, action: "no_trade", rationale: args.rationale };
    return { content: [{ type: "text", text: "recorded" }] };
  });
  const server = createSdkMcpServer({
    name: SDK_SERVER_NAME,
    version: "1.0.0",
    tools: [submitOrderIntent, noTrade],
  });

  const abortController = new AbortController();
  const timer = setTimeout(() => abortController.abort(), policy.timeoutMinutes * 60_000);
  timer.unref?.();

  return {
    tools: [],
    mcpServers: { [SDK_SERVER_NAME]: server },
    allowedTools: [...ALLOWED_LIVE_TOOLS],
    permissionMode: "dontAsk",
    hooks: { PreToolUse: [{ hooks: [preToolUseHook] }] },
    maxTurns: policy.maxTurns,
    maxBudgetUsd: policy.maxBudgetUsd,
    outputFormat: { type: "json_schema", schema: loadRawSchema("decision") },
    abortController,
    model: policy.model,
    persistSession: false,
  };
}

export interface LiveDeciderOptions {
  /**
   * Caps this instance's per-decision budget at or below whatever `ai_gate.py` allows, never
   * above it -- issue #54's ensemble uses this to split one total budget equally across N
   * members (`splitBudgetEqually` in `./ensemble.js`), so the panel's combined worst-case spend
   * is the same as a single agent's, not N times it.
   */
  maxBudgetUsdOverride?: number;
  /** Defaults to `resolvePromptVersion(process.env)` (QC_PROMPT_VERSION, validated, fail-closed)
   *  when omitted -- an ensemble member can be pinned to a specific version explicitly instead. */
  promptVersion?: string;
}

export class LiveDecider implements Decider {
  readonly promptVersion: string;

  constructor(private readonly opts: LiveDeciderOptions = {}) {
    this.promptVersion =
      opts.promptVersion !== undefined
        ? requireKnownPromptVersion(opts.promptVersion, "LiveDeciderOptions.promptVersion")
        : resolvePromptVersion();
  }

  async decide(req: DecisionRequest): Promise<DecisionOutcome> {
    const policy = resolveLivePolicy();
    if (this.opts.maxBudgetUsdOverride !== undefined) {
      // Tighten only, matching root CLAUDE.md rule 4's "limits can only be tightened
      // automatically" logic even though this isn't a config/limits/** risk limit: a caller-
      // supplied override can never raise what ai_gate.py already allowed.
      policy.maxBudgetUsd = Math.min(policy.maxBudgetUsd, this.opts.maxBudgetUsdOverride);
    }
    const capture: { outcome?: Decision } = {};
    const options = buildOptions(policy, capture);
    const prompt = buildPrompt(req, this.promptVersion);

    const raw: unknown[] = [];
    let costUsd: DecimalString | undefined;
    let structuredOutput: Decision | undefined;
    for await (const message of query({ prompt, options })) {
      raw.push(message);
      if (message.type === "result" && message.subtype === "success") {
        // The SDK hands this back as a JS number; the ledger's cost_usd is a decimal string
        // (root CLAUDE.md "no floats for money") -- convert at the boundary, once, here.
        costUsd = usdToDecimalString(message.total_cost_usd);
        if (message.structured_output) structuredOutput = message.structured_output as Decision;
      }
    }

    const decision = capture.outcome ?? structuredOutput;
    if (!decision) {
      throw new Error(`live decider produced no decision for request ${req.request_id}`);
    }
    return { decision, raw, costUsd };
  }
}
