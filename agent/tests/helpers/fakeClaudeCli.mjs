#!/usr/bin/env node
// Fake Claude Code CLI: stands in for the real bundled executable that
// `@anthropic-ai/claude-agent-sdk`'s query() spawns and talks to over
// stream-json on stdin/stdout, so a test can drive the SDK's actual
// PreToolUse-hook / permission-decision code path with no network call and
// no real model. Point `options.pathToClaudeCodeExecutable` at this file and
// `options.executable` at "node" (see agent/tests/permission-e2e.test.ts).
//
// This implements only the slice of the protocol needed to submit tool_use
// attempts and read back the SDK's real permission decision for each one:
//   1. control_request{subtype:"initialize"} -> control_response{success},
//      capturing the PreToolUse hookCallbackId and registered SDK MCP server
//      names the real Options object declared.
//   2. system/init, then one user message so query() starts a turn.
//   3. For each entry in the plan (read from FAKE_CLI_PLAN, a JSON array of
//      {tool_name, tool_input}): emit an assistant message with a tool_use
//      block for it, then ask the SDK to run its real PreToolUse hook via a
//      control_request{subtype:"hook_callback"} (this is the same call path
//      the real CLI uses; the hook itself runs in the SDK-consumer process,
//      i.e. the code under test, not in this stub).
//      - denied: reply with an error tool_result, move to the next entry.
//      - allowed + an "mcp__<server>__<tool>" name: forward the call to the
//        in-process SDK MCP server via control_request{subtype:"mcp_message"}
//        (tools/call), so the real wrapper-tool handler actually runs, then
//        reply with the tool_result the SDK produced.
//   4. After the plan is exhausted, emit a `result` message and exit.
//
// Every decision (`allow`/`deny` + reason) is appended to the file named by
// FAKE_CLI_LOG (JSON lines), so the test can assert on it without re-parsing
// the raw SDKMessage stream.
import { appendFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

const plan = JSON.parse(process.env.FAKE_CLI_PLAN ?? "[]");
const logPath = process.env.FAKE_CLI_LOG;

function logDecision(entry) {
  if (!logPath) return;
  appendFileSync(logPath, `${JSON.stringify(entry)}\n`, "utf8");
}

function write(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

let hookCallbackId;
let planIndex = 0;
const pendingHook = new Map(); // control request_id -> plan entry
const pendingMcp = new Map(); // JSON-RPC id -> plan entry

function driveNext() {
  if (planIndex >= plan.length) {
    write({
      type: "result",
      subtype: "success",
      is_error: false,
      duration_ms: 1,
      duration_api_ms: 1,
      num_turns: plan.length,
      session_id: "fake-session",
      total_cost_usd: 0,
      usage: { input_tokens: 1, output_tokens: 1 },
      result: "done",
      uuid: randomUUID(),
    });
    return;
  }
  const entry = plan[planIndex++];
  const toolUseId = `tu_${planIndex}`;
  write({
    type: "assistant",
    session_id: "fake-session",
    uuid: randomUUID(),
    parent_tool_use_id: null,
    message: {
      id: `msg_${planIndex}`,
      type: "message",
      role: "assistant",
      model: "fake-model",
      content: [{ type: "tool_use", id: toolUseId, name: entry.tool_name, input: entry.tool_input ?? {} }],
      stop_reason: "tool_use",
      stop_sequence: null,
      usage: { input_tokens: 1, output_tokens: 1 },
    },
  });
  if (!hookCallbackId) {
    // No PreToolUse hook was registered at all: nothing gates this call. Record it as such
    // rather than silently hanging, so a misconfigured Options object fails the test loudly.
    logDecision({ tool_name: entry.tool_name, decision: "no_hook_registered" });
    write({
      type: "user",
      session_id: "fake-session",
      parent_tool_use_id: null,
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: toolUseId, is_error: true, content: "no PreToolUse hook registered" }] },
    });
    driveNext();
    return;
  }
  const requestId = randomUUID();
  pendingHook.set(requestId, { entry, toolUseId });
  write({
    type: "control_request",
    request_id: requestId,
    request: {
      subtype: "hook_callback",
      callback_id: hookCallbackId,
      tool_use_id: toolUseId,
      input: {
        hook_event_name: "PreToolUse",
        tool_name: entry.tool_name,
        tool_input: entry.tool_input ?? {},
        tool_use_id: toolUseId,
        session_id: "fake-session",
        transcript_path: "/tmp/fake-cli-transcript.jsonl",
        cwd: process.cwd(),
      },
    },
  });
}

let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buf += chunk;
  let idx;
  while ((idx = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, idx);
    buf = buf.slice(idx + 1);
    if (!line.trim()) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue; // not this stub's concern; a real CLI would error too
    }
    handle(msg);
  }
});

function handle(msg) {
  if (msg.type === "control_request" && msg.request?.subtype === "initialize") {
    hookCallbackId = msg.request?.hooks?.PreToolUse?.[0]?.hookCallbackIds?.[0];
    write({
      type: "control_response",
      response: { subtype: "success", request_id: msg.request_id, response: { commands: [], output_style: "default" } },
    });
    write({
      type: "system",
      subtype: "init",
      apiKeySource: "ANTHROPIC_API_KEY",
      claude_code_version: "0.0.0-fake",
      cwd: process.cwd(),
      tools: [],
      mcp_servers: (msg.request?.sdkMcpServers ?? []).map((name) => ({ name, status: "connected" })),
      model: "fake-model",
      permissionMode: "dontAsk",
      slash_commands: [],
      output_style: "default",
      skills: [],
      plugins: [],
      uuid: randomUUID(),
      session_id: "fake-session",
    });
    return;
  }

  if (msg.type === "control_response" && pendingHook.has(msg.response?.request_id)) {
    const { entry, toolUseId } = pendingHook.get(msg.response.request_id);
    pendingHook.delete(msg.response.request_id);
    const hookOut = msg.response?.response?.hookSpecificOutput;
    const decision = hookOut?.permissionDecision;
    logDecision({ tool_name: entry.tool_name, decision, reason: hookOut?.permissionDecisionReason });

    if (decision === "allow" && entry.tool_name.startsWith("mcp__")) {
      const [, serverName, ...toolNameParts] = entry.tool_name.split("__");
      const rpcId = randomUUID();
      pendingMcp.set(rpcId, { entry, toolUseId });
      write({
        type: "control_request",
        request_id: randomUUID(),
        request: {
          subtype: "mcp_message",
          server_name: serverName,
          message: { jsonrpc: "2.0", id: rpcId, method: "tools/call", params: { name: toolNameParts.join("__"), arguments: entry.tool_input ?? {} } },
        },
      });
      return;
    }

    write({
      type: "user",
      session_id: "fake-session",
      parent_tool_use_id: null,
      message: {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: toolUseId,
            is_error: decision !== "allow",
            content: decision === "allow" ? "ok" : `denied: ${hookOut?.permissionDecisionReason ?? "no reason given"}`,
          },
        ],
      },
    });
    driveNext();
    return;
  }

  if (msg.type === "control_response" && msg.response?.response?.mcp_response) {
    const rpcId = msg.response.response.mcp_response.id;
    const pending = pendingMcp.get(rpcId);
    if (!pending) return;
    pendingMcp.delete(rpcId);
    write({
      type: "user",
      session_id: "fake-session",
      parent_tool_use_id: null,
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: pending.toolUseId, is_error: false, content: "ok" }] },
    });
    driveNext();
    return;
  }

  if (msg.type === "user" && planIndex === 0) {
    driveNext();
  }
}

// Safety net only: every real path above ends by calling driveNext() through
// to the final `result` message, after which query() closes stdin and this
// process exits on its own. This just stops a protocol mismatch from
// hanging a CI job forever.
setTimeout(() => process.exit(1), 10_000).unref();
