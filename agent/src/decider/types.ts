import type { DecimalString, Decision, DecisionRequest } from "../types.js";

export interface DecisionOutcome {
  decision: Decision;
  /** Full SDK request/response for the ledger's `raw` field; absent in fake mode. */
  raw?: unknown;
  /** Decimal string, matching DecisionLedgerEntry.cost_usd (wave 2: was a JS number). */
  costUsd?: DecimalString;
}

export interface Decider {
  decide(req: DecisionRequest): Promise<DecisionOutcome>;
}
