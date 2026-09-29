// Deterministic rule decider (QC_AGENT_MODE=fake): no network, no API key, same input always
// gives the same output. Rule: if there's a signal, its direction is up/down, and its max
// probability clears the threshold, propose a limit order at the best bid/ask sized within the
// request's own limits; otherwise no_trade.
import { absFixed, fromFixed, toFixed } from "../decimal.js";
import type { Decision, DecisionRequest } from "../types.js";
import type { Decider, DecisionOutcome } from "./types.js";

export interface FakeDeciderOptions {
  /** Minimum max(probs) to act on the signal; below this, no_trade. */
  probThreshold?: number;
  /** Requested order quantity before clamping to the request's limits. */
  qty?: string;
}

const DEFAULT_PROB_THRESHOLD = 0.6;
const DEFAULT_QTY = "1";

export class FakeDecider implements Decider {
  private readonly probThreshold: number;
  private readonly qty: string;

  constructor(opts: FakeDeciderOptions = {}) {
    this.probThreshold = opts.probThreshold ?? DEFAULT_PROB_THRESHOLD;
    this.qty = opts.qty ?? DEFAULT_QTY;
  }

  // async for interface parity with LiveDecider, though this rule never actually awaits anything.
  async decide(req: DecisionRequest): Promise<DecisionOutcome> {
    return { decision: this.decideSync(req) };
  }

  private decideSync(req: DecisionRequest): Decision {
    const { signal } = req;
    if (!signal || signal.direction === "flat") {
      return noTrade(req, "no signal, or signal direction is flat");
    }
    const maxProb = Math.max(...signal.probs);
    if (maxProb < this.probThreshold) {
      return noTrade(req, `max probability ${maxProb.toFixed(4)} below threshold ${this.probThreshold}`);
    }
    const action = signal.direction === "up" ? "buy" : "sell";
    if (!req.allowed_actions.includes(action)) {
      return noTrade(req, `${action} is not in allowed_actions`);
    }
    const qty = clampQty(this.qty, req);
    if (qty <= 0n) {
      return noTrade(req, "requested quantity clamps to zero under current limits");
    }
    return {
      request_id: req.request_id,
      action,
      qty: fromFixed(qty),
      limit_price: action === "buy" ? req.best_ask : req.best_bid,
      confidence: maxProb,
      rationale: `signal ${signal.direction} at max prob ${maxProb.toFixed(4)} >= threshold ${this.probThreshold}`,
    };
  }
}

function noTrade(req: DecisionRequest, reason: string): Decision {
  return { request_id: req.request_id, action: "no_trade", rationale: reason };
}

// ponytail: clamps by max_position and max_notional only, not remaining_daily_loss or order
// rate. That's fine here because this decider's output is a proposal, never an instruction —
// qc-bridge's RiskEngine independently re-checks every limit before anything reaches a venue.
function clampQty(requested: string, req: DecisionRequest): bigint {
  let qty = toFixed(requested);

  const positionRoom = absFixed(toFixed(req.limits.max_position)) - absFixed(toFixed(req.position));
  if (positionRoom < qty) qty = positionRoom > 0n ? positionRoom : 0n;

  const price = toFixed(req.mid);
  if (price > 0n) {
    const maxNotional = absFixed(toFixed(req.limits.max_notional));
    const notionalRoom = (maxNotional * 100_000_000n) / price; // scale 1e8, matches decimal.ts
    if (notionalRoom < qty) qty = notionalRoom;
  }

  return qty > 0n ? qty : 0n;
}
