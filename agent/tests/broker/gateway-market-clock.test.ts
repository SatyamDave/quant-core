// wave2-spec.md gap: "Gateway checks stamp expiry with Date.now() but the protocol says the
// market-data clock." schemas/decision/v1/README.md's v1.1 section is explicit that an
// approval's `expires_ts_ns` must be checked against the bridge's own market/replay clock, never
// an independent wall-clock read -- clock skew between the two processes could otherwise
// silently shrink or extend the 5-second TTL window.
//
// This proves the contract with a market timestamp chosen to be nothing like real wall-clock
// time: if BrokerGateway (or whatever constructs it in production -- see
// agent/src/broker/external-mode.ts) ever used Date.now() instead of the injected market clock,
// this test would fail, because the approval's expires_ts_ns sits entirely within a time window
// far in the past relative to the real clock.
import { generateKeyPairSync, sign as cryptoSign } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { ed25519PublicKeyFromRaw } from "../../src/broker/crypto.js";
import { BrokerGateway } from "../../src/broker/gateway.js";
import { MockBroker } from "../../src/testkit/mock-broker.js";
import type { Approval, ApprovalPayload, ApprovedOrder } from "../../src/broker/types.js";

const { publicKey, privateKey } = generateKeyPairSync("ed25519");
const rawPublicKey = (publicKey.export({ type: "spki", format: "der" }) as Buffer).subarray(-32);

// An arbitrary instant nowhere near real wall-clock "now" at any point this suite will ever run
// (a small handful of seconds after the Unix epoch) -- chosen specifically so that using
// Date.now() instead of this injected market clock produces a wildly different, and wrong,
// answer.
const MARKET_TS_NS = 5_000_000_000; // 5s after epoch
const TTL_NS = 5_000_000_000; // protocol v1.1's fixed 5s TTL

function canonicalPayload(fields: ApprovalPayload): string {
  const sorted: ApprovalPayload = {
    client_order_id: fields.client_order_id,
    expires_ts_ns: fields.expires_ts_ns,
    instrument: fields.instrument,
    limit_price: fields.limit_price,
    qty: fields.qty,
    request_id: fields.request_id,
    side: fields.side,
    time_in_force: fields.time_in_force,
  };
  return JSON.stringify(sorted);
}

function approve(fields: ApprovalPayload): Approval {
  const payload = canonicalPayload(fields);
  const signature = cryptoSign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64");
  return { payload, signature };
}

function orderFrom(fields: ApprovalPayload): ApprovedOrder {
  return {
    client_order_id: fields.client_order_id,
    instrument: fields.instrument,
    side: fields.side,
    qty: fields.qty,
    limit_price: fields.limit_price,
    time_in_force: fields.time_in_force,
    request_id: fields.request_id,
  };
}

function tempJournalPath(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "gateway-market-clock-"));
  return path.join(dir, "broker-journal.jsonl");
}

describe("BrokerGateway approval expiry uses the bridge's market clock, not Date.now()", () => {
  async function makeGateway(now: () => number): Promise<BrokerGateway> {
    return new BrokerGateway({
      broker: new MockBroker(),
      reporter: { reportExecution: async () => ({ ok: true as const }) },
      approvalPublicKey: ed25519PublicKeyFromRaw(rawPublicKey),
      now,
      requestTimeoutMs: 200,
      iocCancelAfterMs: 10,
      journalPath: tempJournalPath(),
    });
  }

  it("accepts an approval that is fresh by the market clock, even though it is ancient by Date.now()", async () => {
    const fields: ApprovalPayload = {
      client_order_id: 101,
      expires_ts_ns: MARKET_TS_NS + TTL_NS,
      instrument: "SPY",
      limit_price: "20.00",
      qty: "1",
      request_id: "req-101",
      side: "buy",
      time_in_force: "ioc",
    };
    // 1 second after the decision, still inside the 5s TTL -- by the market clock this is not
    // expired. Real Date.now() right now is on the order of 1.7e18 ns, so a gateway that used it
    // instead would see expires_ts_ns (5e9-ish) as already long past and reject as "expired".
    const gateway = await makeGateway(() => MARKET_TS_NS + 1_000_000_000);

    const result = await gateway.submitOrder(orderFrom(fields), approve(fields), "equity");
    expect(result.status).not.toBe("rejected");
  });

  it("rejects the same approval once the market clock itself passes expiry", async () => {
    const fields: ApprovalPayload = {
      client_order_id: 102,
      expires_ts_ns: MARKET_TS_NS + TTL_NS,
      instrument: "SPY",
      limit_price: "20.00",
      qty: "1",
      request_id: "req-102",
      side: "buy",
      time_in_force: "ioc",
    };
    // Past the TTL by the market clock -- must be refused even though this instant is still
    // absurdly "early" by Date.now()'s standard.
    const gateway = await makeGateway(() => MARKET_TS_NS + TTL_NS + 1);

    await expect(gateway.submitOrder(orderFrom(fields), approve(fields), "equity")).rejects.toThrow(/expired/);
  });
});
