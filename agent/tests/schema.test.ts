import { describe, expect, it } from "vitest";
import { validateOrThrow } from "../src/schema.js";

const VALID_DECISION_REQUEST = {
  request_id: "req-1",
  ts_ns: 1767571200000000000,
  instrument: "SIM-BTC",
  best_bid: "65000.0",
  best_ask: "65000.1",
  mid: "65000.05",
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
};

describe("schema validation", () => {
  it("accepts a valid decision_request", () => {
    expect(() => validateOrThrow("decision_request", VALID_DECISION_REQUEST)).not.toThrow();
  });

  it("rejects a decision_request missing a required field", () => {
    const { limits, ...missingLimits } = VALID_DECISION_REQUEST;
    expect(() => validateOrThrow("decision_request", missingLimits)).toThrow(/schema validation failed/);
  });

  it("rejects a decision_request with a float quantity instead of a decimal string", () => {
    const bad = { ...VALID_DECISION_REQUEST, best_bid: 65000.0 };
    expect(() => validateOrThrow("decision_request", bad)).toThrow(/schema validation failed/);
  });

  it("rejects an unknown additional property", () => {
    const bad = { ...VALID_DECISION_REQUEST, extra_field: "not allowed" };
    expect(() => validateOrThrow("decision_request", bad)).toThrow(/schema validation failed/);
  });

  it("rejects a bridge_request with an unknown op", () => {
    expect(() => validateOrThrow("bridge_request", { v: 1, id: "1", op: "delete_everything" })).toThrow();
  });

  it("rejects a bridge_request for submit_order_intent missing the intent", () => {
    expect(() => validateOrThrow("bridge_request", { v: 1, id: "1", op: "submit_order_intent" })).toThrow();
  });

  it("accepts a valid decision", () => {
    expect(() =>
      validateOrThrow("decision", { request_id: "req-1", action: "no_trade", rationale: "flat" }),
    ).not.toThrow();
  });

  it("rejects a buy decision missing qty/limit_price", () => {
    expect(() =>
      validateOrThrow("decision", { request_id: "req-1", action: "buy", rationale: "missing sizing" }),
    ).toThrow();
  });
});
