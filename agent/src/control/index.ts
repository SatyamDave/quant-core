// The "agent computer" control core: one small surface an operator's agent uses to read status
// and rules, tighten rules, read its own decisions, submit an order intent, and pull the kill
// switch. It reuses what already exists -- qc-bridge (BridgeClient) for every order, the
// append-only decision ledger for history, qc-bridge's --kill-file for the kill switch -- and
// adds no second path to a broker. qc-bridge and qc-risk stay authoritative (root rule 1).
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import path from "node:path";
import type { BridgeClient } from "../bridge.js";
import { fromFixed, toFixed } from "../decimal.js";
import { ledgerPath, repoRoot } from "../paths.js";
import type { DecisionLedgerEntry, IntentResult, OrderIntent } from "../types.js";

export type ControlMode = "sim" | "paper" | "external";
export type ControlBridge = Pick<BridgeClient, "hello" | "status" | "submitOrderIntent">;

export interface ControlOptions {
  /** A started qc-bridge. Without one, status reports only what is on disk. */
  bridge?: ControlBridge;
  /** Limits TOML; default config/limits/<instrument>.toml, else config/limits/default.toml. */
  limitsFile?: string;
  instrument?: string;
  /** Same file qc-bridge polls via --kill-file; default $QC_KILL_FILE or ops/live/state/KILL
   *  (config/environments/canary.toml's kill_file). */
  killFile?: string;
  ledgerFile?: string;
  now?: () => Date;
}

/** Every key in config/limits/*.toml and which direction is tighter. Unknown keys are refused. */
const TIGHTER: Record<string, "lower" | "higher"> = {
  max_position: "lower",
  max_notional: "lower",
  max_order_rate_per_sec: "lower",
  max_daily_loss: "lower",
  price_band_bps: "lower",
  stale_data_ms: "lower",
  max_daily_notional: "lower",
  wash_trade_window_ms: "higher", // a wider window flags more wash-trade patterns
};

export type Rules = Record<string, string>;

export interface RuleChange {
  key: string;
  value: string;
}

export interface Status {
  mode: ControlMode;
  killSwitch: { engaged: boolean; file: string };
  halted: string | null;
  position: string | null;
  today: { date: string; decisions: number; ordersAccepted: number; notional: string };
  limits: Rules;
}

function limitsFileFor(opts: ControlOptions): string {
  if (opts.limitsFile) return opts.limitsFile;
  const dir = path.join(repoRoot(), "config", "limits");
  const perInstrument = opts.instrument ? path.join(dir, `${opts.instrument.toLowerCase()}.toml`) : undefined;
  return perInstrument && existsSync(perInstrument) ? perInstrument : path.join(dir, "default.toml");
}

function killFileFor(opts: ControlOptions): string {
  return opts.killFile ?? process.env.QC_KILL_FILE ?? path.join(repoRoot(), "ops", "live", "state", "KILL");
}

const LINE = /^(\s*)([a-z_]+)(\s*=\s*)("?)([0-9.]+)("?)(.*)$/;

/** Reads the flat `key = value` limits file (ponytail: no TOML dependency; the limits files are
 *  flat scalars -- switch to a real parser if they ever grow tables). Values come back as
 *  decimal strings. */
export function getRules(opts: ControlOptions = {}): Rules {
  const rules: Rules = {};
  for (const line of readFileSync(limitsFileFor(opts), "utf8").split("\n")) {
    const m = LINE.exec(line);
    if (m?.[2] && m[5]) rules[m[2]] = m[5];
  }
  return rules;
}

/** Applies a tightening change to the limits file at once (root rule 4 allows automatic
 *  tightening); throws on any loosening, unknown key or malformed value. qc-bridge reads limits
 *  at startup, so a running bridge enforces the new value from its next restart. */
export function proposeRuleChange(change: RuleChange, opts: ControlOptions = {}): { key: string; from: string; to: string } {
  const file = limitsFileFor(opts);
  const direction = TIGHTER[change.key];
  if (!direction) throw new Error(`unknown limit ${JSON.stringify(change.key)}; known: ${Object.keys(TIGHTER).join(", ")}`);
  const current = getRules(opts)[change.key];
  if (current === undefined) throw new Error(`${change.key} is not set in ${file}; adding a limit needs a human edit`);
  const next = toFixed(change.value); // throws on a non-decimal
  if (next < 0n) throw new Error(`${change.key} cannot be negative`);
  const cur = toFixed(current);
  const loosens = direction === "lower" ? next > cur : next < cur;
  if (loosens) {
    throw new Error(
      `refused: ${change.key} ${current} -> ${change.value} loosens a risk limit. Limits are tighten-only ` +
        `for agents (root CLAUDE.md rule 4); a human must edit ${path.relative(repoRoot(), file)} with two approvals.`,
    );
  }
  const text = readFileSync(file, "utf8")
    .split("\n")
    .map((line) => {
      const m = LINE.exec(line);
      return m && m[2] === change.key ? `${m[1]}${m[2]}${m[3]}${m[4]}${change.value}${m[6]}${m[7]}` : line;
    })
    .join("\n");
  const tmp = `${file}.tmp`;
  writeFileSync(tmp, text, "utf8");
  renameSync(tmp, file); // atomic: qc-bridge never reads a half-written limits file
  return { key: change.key, from: current, to: change.value };
}

function readLedger(opts: ControlOptions): DecisionLedgerEntry[] {
  const file = opts.ledgerFile ?? ledgerPath();
  if (!existsSync(file)) return [];
  return readFileSync(file, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as DecisionLedgerEntry);
}

/** Newest first. */
export function listDecisions(limit = 20, opts: ControlOptions = {}): DecisionLedgerEntry[] {
  return readLedger(opts).slice(-limit).reverse();
}

/** Ledger entries whose decision was a trade the bridge accepted, newest first. */
export function listOrders(limit = 20, opts: ControlOptions = {}): DecisionLedgerEntry[] {
  return readLedger(opts)
    .filter((e) => e.result?.accepted)
    .slice(-limit)
    .reverse();
}

export function killSwitchState(opts: ControlOptions = {}): { engaged: boolean; file: string } {
  const file = killFileFor(opts);
  return { engaged: existsSync(file), file };
}

/** Creates the kill file qc-bridge's watcher polls (halts within 1s, cancels working orders).
 *  One-way on purpose: only a human deletes the file and restarts the bridge. */
export function engageKillSwitch(reason = "engaged by agent control", opts: ControlOptions = {}): { engaged: true; file: string } {
  const file = killFileFor(opts);
  mkdirSync(path.dirname(file), { recursive: true });
  if (!existsSync(file)) writeFileSync(file, `${new Date().toISOString()} ${reason}\n`, "utf8");
  return { engaged: true, file };
}

async function modeOf(bridge: ControlBridge | undefined): Promise<ControlMode> {
  if (!bridge) return "sim";
  const hello = await bridge.hello();
  if (hello.hello?.venue_mode === "external") return "external";
  // SimVenue fills against a live-following recording is paper trading.
  return (process.env.QC_BRIDGE_ARGS ?? "").split(" ").includes("--follow") ? "paper" : "sim";
}

export async function getStatus(opts: ControlOptions = {}): Promise<Status> {
  const date = (opts.now?.() ?? new Date()).toISOString().slice(0, 10);
  // ponytail: "today" is the UTC date of the request's ts_ns, not the exchange day qc-risk uses.
  const todays = readLedger(opts).filter((e) => new Date(e.request.ts_ns / 1e6).toISOString().startsWith(date));
  let notional = 0n;
  let ordersAccepted = 0;
  for (const e of todays) {
    if (!e.result?.accepted || !e.decision.qty || !e.decision.limit_price) continue;
    ordersAccepted++;
    notional += (toFixed(e.decision.qty) * toFixed(e.decision.limit_price)) / 100_000_000n;
  }
  const status = opts.bridge ? (await opts.bridge.status()).status : undefined;
  return {
    mode: await modeOf(opts.bridge),
    killSwitch: killSwitchState(opts),
    halted: status?.halted ?? null,
    position: status?.position ?? null,
    today: { date, decisions: todays.length, ordersAccepted, notional: fromFixed(notional) },
    limits: getRules(opts),
  };
}

/** Submits through qc-bridge's risk-wrapped submit_order_intent -- never to a broker. Refuses an
 *  external-venue bridge unless `allowExternal` is set, so the default is sim/paper. In external
 *  mode the result is only an approval; the broker gateway (src/broker/) executes it. */
export async function submitOrderIntent(
  intent: OrderIntent,
  opts: ControlOptions & { bridge: ControlBridge; allowExternal?: boolean },
): Promise<IntentResult> {
  if (killSwitchState(opts).engaged) return { accepted: false, halted: "kill_switch" };
  if ((await modeOf(opts.bridge)) === "external" && !opts.allowExternal) {
    throw new Error("refused: the bridge is in external venue mode; pass allowExternal to route a real order");
  }
  const resp = await opts.bridge.submitOrderIntent(intent);
  if (!resp.ok || !resp.result) throw new Error(`qc-bridge rejected the request: ${resp.error?.message ?? "no result"}`);
  return resp.result;
}
