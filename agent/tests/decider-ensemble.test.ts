// Issue #54: unit tests for the aggregation logic itself, independent of any real member decider
// (no network, no API key -- root CLAUDE.md hard limit). Members are hand-built Decider stubs so
// each test controls exactly what "the panel" proposed.
import { describe, expect, it } from "vitest";
import {
  aggregate,
  EnsembleDecider,
  parseEnsembleEnv,
  splitBudgetEqually,
  sumCostUsd,
  type EnsembleMember,
} from "../src/decider/ensemble.js";
import type { Decider, DecisionOutcome } from "../src/decider/types.js";
import type { Decision, DecisionRequest } from "../src/types.js";

function baseRequest(overrides: Partial<DecisionRequest> = {}): DecisionRequest {
  return {
    request_id: "req-1",
    ts_ns: 1,
    instrument: "SIM-BTC",
    best_bid: "100.0",
    best_ask: "100.1",
    mid: "100.05",
    spread_ticks: 1,
    features: {},
    signal: null,
    position: "0",
    limits: {
      max_position: "10",
      max_notional: "100000",
      max_order_rate_per_sec: 5,
      remaining_daily_loss: "1000",
    },
    allowed_actions: ["buy", "sell", "no_trade"],
    ...overrides,
  };
}

function decision(overrides: Partial<Decision> = {}): Decision {
  return { request_id: "req-1", action: "no_trade", rationale: "stub", ...overrides };
}

/** A member that always returns the same outcome, regardless of the request -- lets tests build
 *  a panel with an exact, known vote per member. */
class StubDecider implements Decider {
  constructor(private readonly outcome: DecisionOutcome) {}
  async decide(): Promise<DecisionOutcome> {
    return this.outcome;
  }
}

function stubMember(label: string, outcome: DecisionOutcome): EnsembleMember {
  return { label, decider: new StubDecider(outcome) };
}

describe("aggregate: unanimous", () => {
  it("proposes the trade when every member agrees", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "1", limit_price: "100.1" }),
      decision({ action: "buy", qty: "3", limit_price: "100.1" }),
      decision({ action: "buy", qty: "2", limit_price: "100.1" }),
    ];
    const result = aggregate(req, members, "unanimous");
    expect(result.action).toBe("buy");
    expect(result.qty).toBe("2"); // median of 1, 2, 3
    expect(result.limit_price).toBe(req.best_ask);
  });

  it("falls back to no_trade when even one member dissents", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "1", limit_price: "100.1" }),
      decision({ action: "buy", qty: "1", limit_price: "100.1" }),
      decision({ action: "no_trade" }),
    ];
    expect(aggregate(req, members, "unanimous").action).toBe("no_trade");
  });

  it("falls back to no_trade when members disagree on side", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "1", limit_price: "100.1" }),
      decision({ action: "sell", qty: "1", limit_price: "100.0" }),
    ];
    expect(aggregate(req, members, "unanimous").action).toBe("no_trade");
  });
});

describe("aggregate: majority", () => {
  it("proposes the plurality action", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "2", limit_price: "100.1" }),
      decision({ action: "buy", qty: "4", limit_price: "100.1" }),
      decision({ action: "sell", qty: "1", limit_price: "100.0" }),
    ];
    const result = aggregate(req, members, "majority");
    expect(result.action).toBe("buy");
    expect(result.qty).toBe("3"); // median of the two buy votes: 2, 4
  });

  it("defaults to no_trade on an exact tie (no strict plurality)", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "1", limit_price: "100.1" }),
      decision({ action: "sell", qty: "1", limit_price: "100.0" }),
      decision({ action: "no_trade" }),
    ];
    expect(aggregate(req, members, "majority").action).toBe("no_trade");
  });

  it("defaults to no_trade when no_trade itself has the plurality", () => {
    const req = baseRequest();
    const members = [decision({ action: "no_trade" }), decision({ action: "no_trade" }), decision({ action: "buy", qty: "1", limit_price: "100.1" })];
    expect(aggregate(req, members, "majority").action).toBe("no_trade");
  });
});

describe("aggregate: confidence_weighted", () => {
  it("a low-confidence majority can be outweighed by one high-confidence dissenter", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "1", limit_price: "100.1", confidence: 0.1 }),
      decision({ action: "buy", qty: "1", limit_price: "100.1", confidence: 0.1 }),
      decision({ action: "sell", qty: "5", limit_price: "100.0", confidence: 0.9 }),
    ];
    const result = aggregate(req, members, "confidence_weighted");
    expect(result.action).toBe("sell");
    expect(result.qty).toBe("5");
  });

  it("treats a missing confidence as equal weight (1.0), same as an unweighted vote", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "1", limit_price: "100.1" }), // no confidence -> weight 1
      decision({ action: "sell", qty: "1", limit_price: "100.0", confidence: 0.5 }),
    ];
    expect(aggregate(req, members, "confidence_weighted").action).toBe("buy");
  });

  it("defaults to no_trade on an exact weighted tie", () => {
    const req = baseRequest();
    const members = [
      decision({ action: "buy", qty: "1", limit_price: "100.1", confidence: 0.5 }),
      decision({ action: "sell", qty: "1", limit_price: "100.0", confidence: 0.5 }),
    ];
    expect(aggregate(req, members, "confidence_weighted").action).toBe("no_trade");
  });
});

describe("aggregate: refuses an empty panel", () => {
  it("throws rather than guessing", () => {
    expect(() => aggregate(baseRequest(), [], "majority")).toThrow(/no member decisions/);
  });
});

describe("EnsembleDecider", () => {
  it("records every member's decision verbatim in raw.members (issue #54)", async () => {
    const req = baseRequest();
    const members = [
      stubMember("a", { decision: decision({ action: "buy", qty: "1", limit_price: "100.1" }), costUsd: "0.01" }),
      stubMember("b", { decision: decision({ action: "buy", qty: "1", limit_price: "100.1" }), costUsd: "0.02" }),
    ];
    const ensemble = new EnsembleDecider(members, "unanimous");
    const outcome = await ensemble.decide(req);

    expect(outcome.decision.action).toBe("buy");
    expect(outcome.costUsd).toBe("0.03");
    const raw = outcome.raw as { aggregation: string; members: { label: string }[] };
    expect(raw.aggregation).toBe("unanimous");
    expect(raw.members.map((m) => m.label)).toEqual(["a", "b"]);
  });

  it("is deterministic given identical member outcomes", async () => {
    const req = baseRequest();
    const members = [
      stubMember("a", { decision: decision({ action: "sell", qty: "2", limit_price: "100.0" }) }),
      stubMember("b", { decision: decision({ action: "sell", qty: "2", limit_price: "100.0" }) }),
    ];
    const ensemble = new EnsembleDecider(members, "majority");
    const [first, second] = await Promise.all([ensemble.decide(req), ensemble.decide(req)]);
    expect(first.decision).toEqual(second.decision);
  });

  it("leaves costUsd undefined when no member reports one (fake-mode convention)", async () => {
    const req = baseRequest();
    const members = [stubMember("a", { decision: decision() }), stubMember("b", { decision: decision() })];
    const outcome = await new EnsembleDecider(members, "majority").decide(req);
    expect(outcome.costUsd).toBeUndefined();
  });

  it("refuses to construct with zero members", () => {
    expect(() => new EnsembleDecider([], "majority")).toThrow(/needs at least one member/);
  });
});

describe("sumCostUsd", () => {
  it("sums decimal-string costs without floats", () => {
    expect(sumCostUsd(["0.1", "0.2", "0.30000001"])).toBe("0.60000001");
  });

  it("returns undefined when every input is undefined", () => {
    expect(sumCostUsd([undefined, undefined])).toBeUndefined();
  });

  it("treats a mix of defined and undefined as the defined ones only", () => {
    expect(sumCostUsd(["1", undefined, "2"])).toBe("3");
  });
});

describe("splitBudgetEqually", () => {
  it("splits a total budget into N equal shares whose sum is the total, exactly", () => {
    expect(splitBudgetEqually(0.3, 3)).toBeCloseTo(0.1, 12);
  });

  it("throws on a non-positive member count", () => {
    expect(() => splitBudgetEqually(1, 0)).toThrow(/memberCount must be positive/);
  });
});

describe("parseEnsembleEnv", () => {
  it("returns undefined when QC_ENSEMBLE_SIZE is unset (no ensemble; today's behavior)", () => {
    expect(parseEnsembleEnv({})).toBeUndefined();
  });

  it("parses a valid size + aggregation", () => {
    expect(parseEnsembleEnv({ QC_ENSEMBLE_SIZE: "3", QC_ENSEMBLE_AGGREGATION: "majority" })).toEqual({
      size: 3,
      aggregation: "majority",
      fakeProbThresholds: undefined,
    });
  });

  it("parses per-member fake prob thresholds", () => {
    const config = parseEnsembleEnv({
      QC_ENSEMBLE_SIZE: "3",
      QC_ENSEMBLE_AGGREGATION: "majority",
      QC_ENSEMBLE_FAKE_PROB_THRESHOLDS: "0.5,0.6,0.7",
    });
    expect(config?.fakeProbThresholds).toEqual([0.5, 0.6, 0.7]);
  });

  it("refuses a non-integer or non-positive size rather than rounding it", () => {
    expect(() => parseEnsembleEnv({ QC_ENSEMBLE_SIZE: "1.5", QC_ENSEMBLE_AGGREGATION: "majority" })).toThrow(
      /positive integer/,
    );
    expect(() => parseEnsembleEnv({ QC_ENSEMBLE_SIZE: "0", QC_ENSEMBLE_AGGREGATION: "majority" })).toThrow(
      /positive integer/,
    );
  });

  it("refuses a size with no aggregation rule rather than guessing one", () => {
    expect(() => parseEnsembleEnv({ QC_ENSEMBLE_SIZE: "3" })).toThrow(/QC_ENSEMBLE_AGGREGATION is not/);
  });

  it("refuses an unknown aggregation rule", () => {
    expect(() =>
      parseEnsembleEnv({ QC_ENSEMBLE_SIZE: "3", QC_ENSEMBLE_AGGREGATION: "debate" }),
    ).toThrow(/must be one of/);
  });

  it("refuses a thresholds list whose length doesn't match the size", () => {
    expect(() =>
      parseEnsembleEnv({
        QC_ENSEMBLE_SIZE: "3",
        QC_ENSEMBLE_AGGREGATION: "majority",
        QC_ENSEMBLE_FAKE_PROB_THRESHOLDS: "0.5,0.6",
      }),
    ).toThrow(/has 2 entries, expected 3/);
  });

  it("refuses a threshold outside (0, 1]", () => {
    expect(() =>
      parseEnsembleEnv({
        QC_ENSEMBLE_SIZE: "1",
        QC_ENSEMBLE_AGGREGATION: "majority",
        QC_ENSEMBLE_FAKE_PROB_THRESHOLDS: "1.5",
      }),
    ).toThrow(/must be in \(0, 1\]/);
  });
});
