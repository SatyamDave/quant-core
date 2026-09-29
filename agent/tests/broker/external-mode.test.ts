// External venue mode refuses to start for an account the broker adapter will not trade in,
// before the bridge is asked anything (BrokerAdapter.verifyAccount).
import { describe, expect, it } from "vitest";
import type { BridgeClient } from "../../src/bridge.js";
import { createExternalModeGateway } from "../../src/broker/external-mode.js";
import { MockBroker, type MockBrokerOptions } from "../../src/testkit/mock-broker.js";

function start(opts: MockBrokerOptions) {
  let hellos = 0;
  const bridge = {
    hello: async () => {
      hellos += 1;
      return { ok: false, error: { code: "unused", message: "the test bridge never answers hello" } };
    },
  } as unknown as BridgeClient;
  const result = createExternalModeGateway({ bridge, broker: new MockBroker(opts), assetClass: "equity", instrument: "SPY" });
  return { result, hellos: () => hellos };
}

describe("createExternalModeGateway account check", () => {
  it("refuses an account the adapter will not trade in before the bridge is asked anything", async () => {
    const { result, hellos } = start({ accountError: "account may not trade" });
    await expect(result).rejects.toThrow(/account may not trade/);
    expect(hellos()).toBe(0);
  });

  it("goes on to the bridge handshake once the account verifies", async () => {
    const { result, hellos } = start({});
    await expect(result).rejects.toThrow(/bridge hello failed/);
    expect(hellos()).toBe(1);
  });
});
