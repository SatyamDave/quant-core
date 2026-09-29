import { describe, expect, it } from "vitest";
import { FakeDecider } from "../src/decider/fake.js";
import type { DecisionRequest } from "../src/types.js";

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

describe("FakeDecider", () => {
  it("is deterministic: same input, same output, run twice", async () => {
    const decider = new FakeDecider();
    const req = baseRequest({
      signal: { direction: "up", probs: [0.1, 0.2, 0.7], model_sha256: "a".repeat(64) },
    });
    const [a, b] = await Promise.all([decider.decide(req), decider.decide(structuredClone(req))]);
    expect(a).toEqual(b);
  });

  it("no_trade when there is no signal", async () => {
    const decider = new FakeDecider();
    const { decision } = await decider.decide(baseRequest({ signal: null }));
    expect(decision.action).toBe("no_trade");
  });

  it("no_trade when the signal direction is flat", async () => {
    const decider = new FakeDecider();
    const { decision } = await decider.decide(
      baseRequest({ signal: { direction: "flat", probs: [0.3, 0.4, 0.3], model_sha256: "a".repeat(64) } }),
    );
    expect(decision.action).toBe("no_trade");
  });

  it("no_trade when max probability is below the threshold", async () => {
    const decider = new FakeDecider({ probThreshold: 0.9 });
    const { decision } = await decider.decide(
      baseRequest({ signal: { direction: "up", probs: [0.1, 0.2, 0.7], model_sha256: "a".repeat(64) } }),
    );
    expect(decision.action).toBe("no_trade");
  });

  it("buys at best_ask when direction is up and probability clears the threshold", async () => {
    const decider = new FakeDecider({ probThreshold: 0.6 });
    const req = baseRequest({
      signal: { direction: "up", probs: [0.1, 0.2, 0.7], model_sha256: "a".repeat(64) },
    });
    const { decision } = await decider.decide(req);
    expect(decision.action).toBe("buy");
    expect(decision.limit_price).toBe(req.best_ask);
    expect(decision.qty).toBeDefined();
  });

  it("sells at best_bid when direction is down and probability clears the threshold", async () => {
    const decider = new FakeDecider({ probThreshold: 0.6 });
    const req = baseRequest({
      signal: { direction: "down", probs: [0.7, 0.2, 0.1], model_sha256: "a".repeat(64) },
    });
    const { decision } = await decider.decide(req);
    expect(decision.action).toBe("sell");
    expect(decision.limit_price).toBe(req.best_bid);
  });

  it("no_trade when the signalled side isn't in allowed_actions", async () => {
    const decider = new FakeDecider({ probThreshold: 0.6 });
    const req = baseRequest({
      signal: { direction: "up", probs: [0.1, 0.2, 0.7], model_sha256: "a".repeat(64) },
      allowed_actions: ["sell", "no_trade"],
    });
    const { decision } = await decider.decide(req);
    expect(decision.action).toBe("no_trade");
  });

  it("clamps quantity to zero, and refuses, when there is no position room left", async () => {
    const decider = new FakeDecider({ probThreshold: 0.6, qty: "5" });
    const req = baseRequest({
      signal: { direction: "up", probs: [0.1, 0.2, 0.7], model_sha256: "a".repeat(64) },
      position: "10",
      limits: {
        max_position: "10",
        max_notional: "100000",
        max_order_rate_per_sec: 5,
        remaining_daily_loss: "1000",
      },
    });
    const { decision } = await decider.decide(req);
    expect(decision.action).toBe("no_trade");
  });
});
