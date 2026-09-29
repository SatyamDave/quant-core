// Localhost-only JSON API over the same operations as mcp-server.ts, for the UI; also serves the
// UI's static files (ui/dist, else ui/) at /. Binds 127.0.0.1 only. No endpoint loosens a limit
// or releases the kill switch.
import { existsSync, readFileSync, statSync } from "node:fs";
import { createServer, type IncomingMessage, type Server } from "node:http";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { repoRoot } from "../paths.js";
import type { ControlOptions } from "./index.js";
import { bridgeFromEnv, OPERATIONS } from "./mcp-server.js";

/** method + path -> operation name, and where its arguments come from. */
const ROUTES: Record<string, string> = {
  "GET /api/status": "get_status",
  "GET /api/rules": "get_rules",
  "POST /api/rules": "propose_rule_change",
  "GET /api/decisions": "list_decisions",
  "GET /api/orders": "list_orders",
  "POST /api/orders": "submit_order_intent",
  "POST /api/kill-switch": "engage_kill_switch",
};

const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
};

async function body(req: IncomingMessage): Promise<Record<string, unknown>> {
  let text = "";
  for await (const chunk of req) {
    text += chunk;
    if (text.length > 64_000) throw new Error("body too large");
  }
  const parsed: unknown = text ? JSON.parse(text) : {};
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("body must be a JSON object");
  return parsed as Record<string, unknown>;
}

export interface HttpOptions extends ControlOptions {
  uiDir?: string;
}

export function createControlServer(opts: HttpOptions = {}): Server {
  const uiRoot = opts.uiDir ?? [path.join(repoRoot(), "ui", "dist"), path.join(repoRoot(), "ui")].find((d) => existsSync(d));
  return createServer(async (req, res) => {
    const send = (code: number, payload: unknown, type = "application/json") => {
      res.writeHead(code, { "content-type": type, "cache-control": "no-store" });
      res.end(type === "application/json" ? JSON.stringify(payload) : (payload as Buffer));
    };
    // DNS-rebinding guard: a hostile page resolving its own name to 127.0.0.1 sends its own Host.
    const host = (req.headers.host ?? "").replace(/:\d+$/, "");
    if (host !== "127.0.0.1" && host !== "localhost") return send(403, { error: "localhost only" });
    const url = new URL(req.url ?? "/", "http://127.0.0.1");

    if (url.pathname.startsWith("/api/")) {
      const name = ROUTES[`${req.method} ${url.pathname}`];
      if (!name) return send(404, { error: "no such endpoint" });
      // Cross-site forms cannot send application/json without a CORS preflight, which this server
      // never answers -- so another origin in the operator's browser cannot trigger a POST.
      if (req.method === "POST" && !String(req.headers["content-type"]).startsWith("application/json")) {
        return send(415, { error: "content-type must be application/json" });
      }
      try {
        const args = req.method === "POST" ? await body(req) : Object.fromEntries(url.searchParams);
        if (typeof args.limit === "string") args.limit = Number(args.limit);
        return send(200, await OPERATIONS[name]!.run(args, opts));
      } catch (err) {
        const message = (err as Error).message;
        return send(message.startsWith("refused") ? 403 : 400, { error: message });
      }
    }

    if (req.method !== "GET" || !uiRoot) return send(404, { error: "not found" });
    let file: string;
    try {
      file = path.join(uiRoot, path.normalize(decodeURIComponent(url.pathname)).replace(/^([/\\]|\.\.)+/, ""));
    } catch {
      return send(400, "bad path", "text/plain");
    }
    const target = file.startsWith(uiRoot) && existsSync(file) && statSync(file).isDirectory() ? path.join(file, "index.html") : file;
    if (!target.startsWith(uiRoot) || !existsSync(target)) return send(404, "not found", "text/plain");
    return send(200, readFileSync(target), TYPES[path.extname(target)] ?? "application/octet-stream");
  });
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  const port = Number(process.env.QC_CONTROL_PORT ?? 7070);
  createControlServer({ bridge: bridgeFromEnv() }).listen(port, "127.0.0.1", () => {
    console.error(`quant-core control API on http://127.0.0.1:${port}`);
  });
}
