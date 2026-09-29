// Issue #54 (+ #53's pipeline): does a panel of agents beat one, at the same total AI-usage
// budget? `llm-trading-eval-research.md` §2 gives two reasons not to assume yes: Tran & Kiela
// (arXiv:2604.02460) find prior multi-agent trading-agent wins were largely uncontrolled compute,
// not architecture; Kim/Garg/Peng/Garg (arXiv:2506.07962) find same-family models agree ~60% of
// the time when both err, so voting among instances of one model is not a variance-reduction
// ensemble. ADR-0040 Challenge #4's resolution: parallel agents ship only if they beat one agent
// at *equal* token budget on the forward eval, measured honestly, never assumed.
//
// This module only aggregates already-produced per-member decisions into one Decision; it takes
// no position on whether ensembling helps -- scripts/eval/ensemble_compare.py is what measures
// that, on the same scenarios, at the same total budget, reporting whether members failed
// together (correlated errors) as well as the aggregate score.
import { absFixed, fromFixed, toFixed } from "../decimal.js";
import type { Action, Decision, DecisionRequest, DecimalString } from "../types.js";
import type { Decider, DecisionOutcome } from "./types.js";

export type AggregationRule = "unanimous" | "majority" | "confidence_weighted";

export const AGGREGATION_RULES: readonly AggregationRule[] = ["unanimous", "majority", "confidence_weighted"];

export interface EnsembleMember {
  /** Reporting label only (e.g. "member-0", a model id, a prompt version) -- never used for
   *  aggregation logic itself, so two members sharing a label still aggregate correctly. */
  label: string;
  decider: Decider;
}

/** One member's outcome, as recorded verbatim into the ledger's `raw` field (issue #54: "per-
 *  member decisions recorded in the ledger raw"). */
export interface EnsembleMemberRecord {
  label: string;
  decision: Decision;
  costUsd?: DecimalString;
  raw?: unknown;
}

export interface EnsembleRaw {
  aggregation: AggregationRule;
  members: EnsembleMemberRecord[];
}

/** Sums fixed-point decimal strings; undefined inputs count as 0. Returns undefined only when
 *  every input is undefined (matches DecisionOutcome.costUsd's own convention: fake/replay
 *  deciders never report a cost at all, rather than reporting a false "0"). */
export function sumCostUsd(costs: readonly (DecimalString | undefined)[]): DecimalString | undefined {
  if (costs.every((c) => c === undefined)) return undefined;
  let total = 0n;
  for (const c of costs) if (c !== undefined) total += toFixed(c);
  return fromFixed(total);
}

/** Splits a total decision-level budget across N ensemble members, equally. Used by callers that
 *  construct N live members from one total budget (issue #54: "a total token/cost budget split
 *  equally") -- kept as a named, tested function so "split equally" is provably exact, not eyeballed. */
export function splitBudgetEqually(totalUsd: number, memberCount: number): number {
  if (memberCount <= 0) throw new Error(`memberCount must be positive, got ${memberCount}`);
  return totalUsd / memberCount;
}

function medianFixed(values: bigint[]): bigint {
  const sorted = [...values].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[mid]!;
  // Even count: integer-average the two middle values (rounds toward zero). This is a proposal
  // only -- the risk engine re-checks the actual qty regardless of how it was aggregated here.
  return (sorted[mid - 1]! + sorted[mid]!) / 2n;
}

/** Confidence-weighted average of fixed-point values, weights scaled to integer parts-per-
 *  million so the arithmetic stays in bigint (root CLAUDE.md: no floats for money). A member with
 *  no reported confidence gets equal weight (1.0), same as an ordinary, unweighted vote. */
function weightedAverageFixed(values: bigint[], weights: number[]): bigint {
  const WEIGHT_SCALE = 1_000_000n;
  let weightedSum = 0n;
  let weightTotal = 0n;
  for (let i = 0; i < values.length; i++) {
    const w = BigInt(Math.round((weights[i] ?? 1) * 1_000_000));
    weightedSum += values[i]! * w;
    weightTotal += w;
  }
  return weightTotal === 0n ? 0n : weightedSum / weightTotal;
}

function voteCounts(decisions: readonly Decision[]): Map<Action, number> {
  const counts = new Map<Action, number>();
  for (const d of decisions) counts.set(d.action, (counts.get(d.action) ?? 0) + 1);
  return counts;
}

/** The single winner if one action strictly has more votes than every other; undefined on a tie
 *  (fail closed: an ambiguous plurality is not a majority, see `aggregateMajority`). */
function plurality(counts: Map<Action, number>): Action | undefined {
  let best: Action | undefined;
  let bestCount = -1;
  let tied = false;
  for (const [action, count] of counts) {
    if (count > bestCount) {
      best = action;
      bestCount = count;
      tied = false;
    } else if (count === bestCount) {
      tied = true;
    }
  }
  return tied ? undefined : best;
}

function noTradeDecision(req: DecisionRequest, rationale: string): Decision {
  return { request_id: req.request_id, action: "no_trade", rationale };
}

function tradeDecision(req: DecisionRequest, action: "buy" | "sell", qty: bigint, rationale: string): Decision {
  return {
    request_id: req.request_id,
    action,
    qty: fromFixed(absFixed(qty)),
    limit_price: action === "buy" ? req.best_ask : req.best_bid,
    rationale: rationale.slice(0, 500),
  };
}

function aggregateUnanimous(req: DecisionRequest, members: readonly Decision[]): Decision {
  const firstAction = members[0]!.action;
  const allAgree = firstAction !== "no_trade" && members.every((m) => m.action === firstAction);
  if (!allAgree) {
    return noTradeDecision(
      req,
      `ensemble unanimous: ${members.length} members did not all propose the same trade (${summarizeVotes(members)})`,
    );
  }
  const qty = medianFixed(members.map((m) => toFixed(m.qty ?? "0")));
  return tradeDecision(
    req,
    firstAction as "buy" | "sell",
    qty,
    `ensemble unanimous: all ${members.length} members proposed ${firstAction}`,
  );
}

function aggregateMajority(req: DecisionRequest, members: readonly Decision[]): Decision {
  const counts = voteCounts(members);
  const winner = plurality(counts);
  if (winner === undefined || winner === "no_trade") {
    return noTradeDecision(
      req,
      `ensemble majority: no action has a strict plurality among ${members.length} members (${summarizeVotes(members)})`,
    );
  }
  const winners = members.filter((m) => m.action === winner);
  const qty = medianFixed(winners.map((m) => toFixed(m.qty ?? "0")));
  return tradeDecision(
    req,
    winner as "buy" | "sell",
    qty,
    `ensemble majority: ${winners.length}/${members.length} members proposed ${winner}`,
  );
}

function aggregateConfidenceWeighted(req: DecisionRequest, members: readonly Decision[]): Decision {
  const weightByAction = new Map<Action, number>();
  for (const m of members) {
    const weight = m.confidence ?? 1;
    weightByAction.set(m.action, (weightByAction.get(m.action) ?? 0) + weight);
  }
  let winner: Action | undefined;
  let bestWeight = -1;
  let tied = false;
  for (const [action, weight] of weightByAction) {
    if (weight > bestWeight) {
      winner = action;
      bestWeight = weight;
      tied = false;
    } else if (weight === bestWeight) {
      tied = true;
    }
  }
  if (winner === undefined || winner === "no_trade" || tied) {
    return noTradeDecision(
      req,
      `ensemble confidence_weighted: no action has a strict confidence-weighted lead among ` +
        `${members.length} members (${summarizeVotes(members)})`,
    );
  }
  const winners = members.filter((m) => m.action === winner);
  const qty = weightedAverageFixed(
    winners.map((m) => toFixed(m.qty ?? "0")),
    winners.map((m) => m.confidence ?? 1),
  );
  return tradeDecision(
    req,
    winner as "buy" | "sell",
    qty,
    `ensemble confidence_weighted: ${winner} led with weight ${bestWeight.toFixed(4)} across ` +
      `${winners.length}/${members.length} members`,
  );
}

function summarizeVotes(members: readonly Decision[]): string {
  const counts = voteCounts(members);
  return [...counts.entries()].map(([action, n]) => `${action}=${n}`).join(",");
}

/** Pure aggregation, exported so aggregation logic is directly testable without spawning any
 *  member decider. `members` must be non-empty (see EnsembleDecider's constructor guard). */
export function aggregate(req: DecisionRequest, members: readonly Decision[], rule: AggregationRule): Decision {
  if (members.length === 0) throw new Error("aggregate: no member decisions to aggregate");
  switch (rule) {
    case "unanimous":
      return aggregateUnanimous(req, members);
    case "majority":
      return aggregateMajority(req, members);
    case "confidence_weighted":
      return aggregateConfidenceWeighted(req, members);
  }
}

export class EnsembleDecider implements Decider {
  constructor(
    private readonly members: readonly EnsembleMember[],
    private readonly aggregation: AggregationRule,
  ) {
    if (members.length === 0) throw new Error("EnsembleDecider needs at least one member");
  }

  async decide(req: DecisionRequest): Promise<DecisionOutcome> {
    const records: EnsembleMemberRecord[] = await Promise.all(
      this.members.map(async ({ label, decider }) => {
        const outcome = await decider.decide(req);
        return { label, decision: outcome.decision, costUsd: outcome.costUsd, raw: outcome.raw };
      }),
    );

    const decision = aggregate(
      req,
      records.map((r) => r.decision),
      this.aggregation,
    );
    const costUsd = sumCostUsd(records.map((r) => r.costUsd));
    const raw: EnsembleRaw = { aggregation: this.aggregation, members: records };
    return { decision, raw, costUsd };
  }
}

export interface EnsembleEnvConfig {
  size: number;
  aggregation: AggregationRule;
  /** Fake-mode only: per-member QC_FAKE_PROB_THRESHOLD, so members can genuinely disagree in
   *  tests/comparisons without a second live model. Length, if given, must equal `size`. */
  fakeProbThresholds?: number[];
}

/** Parses QC_ENSEMBLE_SIZE / QC_ENSEMBLE_AGGREGATION / QC_ENSEMBLE_FAKE_PROB_THRESHOLDS. Returns
 *  undefined when QC_ENSEMBLE_SIZE is unset (no ensemble; today's single-decider behavior,
 *  unchanged) -- fails closed (throws) on any set-but-invalid value rather than silently falling
 *  back to size 1, since a caller who set these env vars clearly intended an ensemble run. */
export function parseEnsembleEnv(env: NodeJS.ProcessEnv = process.env): EnsembleEnvConfig | undefined {
  const rawSize = env.QC_ENSEMBLE_SIZE;
  if (rawSize === undefined) return undefined;

  const size = Number(rawSize);
  if (!Number.isInteger(size) || size < 1) {
    throw new Error(`QC_ENSEMBLE_SIZE must be a positive integer, got ${JSON.stringify(rawSize)}`);
  }

  const rawAggregation = env.QC_ENSEMBLE_AGGREGATION;
  if (rawAggregation === undefined) {
    throw new Error("QC_ENSEMBLE_SIZE is set but QC_ENSEMBLE_AGGREGATION is not; refusing to guess a rule");
  }
  if (!AGGREGATION_RULES.includes(rawAggregation as AggregationRule)) {
    throw new Error(
      `QC_ENSEMBLE_AGGREGATION must be one of ${AGGREGATION_RULES.join(", ")}, got ${JSON.stringify(rawAggregation)}`,
    );
  }

  let fakeProbThresholds: number[] | undefined;
  const rawThresholds = env.QC_ENSEMBLE_FAKE_PROB_THRESHOLDS;
  if (rawThresholds !== undefined) {
    fakeProbThresholds = rawThresholds.split(",").map((s) => {
      const n = Number(s.trim());
      if (!(n > 0 && n <= 1)) {
        throw new Error(`QC_ENSEMBLE_FAKE_PROB_THRESHOLDS entries must be in (0, 1], got ${JSON.stringify(s)}`);
      }
      return n;
    });
    if (fakeProbThresholds.length !== size) {
      throw new Error(
        `QC_ENSEMBLE_FAKE_PROB_THRESHOLDS has ${fakeProbThresholds.length} entries, expected ${size} (QC_ENSEMBLE_SIZE)`,
      );
    }
  }

  return { size, aggregation: rawAggregation as AggregationRule, fakeProbThresholds };
}
