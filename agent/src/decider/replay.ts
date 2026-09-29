// Deterministic replay decider (QC_AGENT_MODE=replay): reproduces a previously recorded Decision
// for a matching DecisionRequest, with zero live model calls — the "every PR, free,
// deterministic" tier from ADR-0040 Decision §8 (Chronicle, arXiv:2609.20625: pin a real or
// curated agent trajectory once, replay it bit-stable forever after). Built from a set of
// recorded DecisionLedgerEntry fixtures; see agent/fixtures/replay/README.md for what they are
// and how they were produced (synthetic, hand-authored — never a real Anthropic API call).
//
// Fails closed (root CLAUDE.md rule 12): a request_id with no matching recording throws rather
// than falling back to a guess, and a recorded request that no longer matches the request it was
// filed under throws too — replay only ever reproduces exactly what was recorded, never a
// best-effort approximation of it.
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { validateOrThrow } from "../schema.js";
import type { DecisionLedgerEntry, DecisionRequest } from "../types.js";
import type { Decider, DecisionOutcome } from "./types.js";

/** Reads and schema-validates every *.json fixture in `dir` (agent/fixtures/replay by default). */
export function loadReplayFixtures(dir: string): DecisionLedgerEntry[] {
  return readdirSync(dir)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => {
      const entry = JSON.parse(readFileSync(path.join(dir, name), "utf8")) as DecisionLedgerEntry;
      validateOrThrow("decision_ledger_entry", entry);
      return entry;
    });
}

export class ReplayDecider implements Decider {
  private readonly byRequestId = new Map<string, DecisionLedgerEntry>();

  constructor(fixtures: DecisionLedgerEntry[]) {
    for (const fixture of fixtures) {
      if (this.byRequestId.has(fixture.request.request_id)) {
        throw new Error(`duplicate replay fixture for request_id ${fixture.request.request_id}`);
      }
      this.byRequestId.set(fixture.request.request_id, fixture);
    }
  }

  // async for interface parity with LiveDecider; replay never actually awaits anything, which is
  // the point (no live model call, no network).
  async decide(req: DecisionRequest): Promise<DecisionOutcome> {
    const recorded = this.byRequestId.get(req.request_id);
    if (!recorded) {
      throw new Error(
        `replay mode has no recorded decision for request_id ${req.request_id}; replay only ` +
          "reproduces requests it has a fixture for, it never derives a new one.",
      );
    }
    // Bit-stable means immune to anything in `req` beyond identifying which fixture to play
    // back — but a request_id whose recorded request no longer matches the one now arriving
    // means the fixture is stale (or the feed is replaying the wrong recording), which is a
    // louder failure than silently answering with an outdated decision.
    if (JSON.stringify(recorded.request) !== JSON.stringify(req)) {
      throw new Error(
        `replay fixture for request_id ${req.request_id} no longer matches the incoming request`,
      );
    }
    return { decision: recorded.decision, raw: recorded.raw ?? null, costUsd: recorded.cost_usd };
  }
}
