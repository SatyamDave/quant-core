# Writing a broker adapter

quant-core ships no real broker adapter. It ships the generic `BrokerAdapter` interface
(`agent/src/broker/adapter.ts`), a gateway that uses it, and a synthetic mock broker
(`agent/src/testkit/mock-broker.ts`) that is the reference implementation. Connecting your own
broker means writing one module that implements the interface.

> Trading with real money is at your own risk. Read the disclaimer in the [README](../../README.md).

## Where the adapter sits

```
agent decider ──► qc-bridge (risk gate, kill switch) ──► signed approval
                                                            │
                     BrokerGateway (verifies the signature, journals, then sends)
                                                            │
                                                  your BrokerAdapter ──► broker API
```

The adapter only ever sees orders the engine has already approved and signed. It is never
registered as an agent tool, and nothing under `agent/src/decider/` may import it
(`agent/tests/broker/no-decider-import.test.ts`). The recorder gets only its read-only half
(`MarketData`: `getQuote`, `getBook`).

## Steps

1. **Create the module** outside `agent/src/decider/`, for example `adapters/my-broker.ts`, and
   default-export an async factory:

   ```ts
   import type { BrokerAdapter } from "../agent/src/broker/adapter.js";

   export default async function create(): Promise<BrokerAdapter> {
     // Read credentials here, from your secret manager or a local, gitignored .env.
     return new MyBrokerAdapter(/* ... */);
   }
   ```

2. **Implement every method** of `BrokerAdapter`: `verifyAccount`, `previewOrder`, `placeOrder`,
   `cancelOrder`, `getOrderById`, `findOrderByRefId`, `listOrders`, `getAccountState`,
   `getQuote`, `getBook`. Use `mock-broker.ts` as the worked example.

3. **Keep the rules** the gateway's safety depends on:
   - **Fail closed.** Throw on any response you cannot parse. Never turn a strange response into
     a default value.
   - **Idempotency.** Send the gateway's `refId` as the venue's client order id or idempotency
     key, and make `findOrderByRefId` find an order by it. The gateway uses this to look up an
     ambiguous submit instead of resending it.
   - **Limit orders only.** The gateway sends whole-share limit orders (`orderRefusal`). Map
     `time_in_force: "ioc"` to a day order; the gateway emulates IOC by cancelling itself.
   - **Honour `opts.timeoutMs`.** Reject when it passes. A rejected call means "outcome unknown".
   - **Report the venue's state,** never the state you expect. The gateway never trusts a
     cancel's response, only the order state read back afterwards.
   - **Throw `BookUnsupportedError`** from `getBook` when the venue has no Level 2 data.
   - **Keep account ids and credentials inside the adapter.** Never put them in errors, logs or
     `raw`: those are logged and written to status files.

4. **Lock down the key.** Trade-only, withdrawals disabled, IP-allowlisted, scoped per strategy
   and per environment. Never give an agent, or any process the agent talks to, a key that can
   move money out of the account.

5. **Point the agent at it:** `QC_BROKER_MODULE=adapters/my-broker.ts`. When `qc-bridge` reports
   `venue_mode: "external"`, `agent/src/cli.ts` loads the module; without it, the agent refuses
   to start.

6. **Test it.** Copy `agent/tests/broker/gateway.test.ts`, point it at your broker's sandbox or
   paper environment, and run `just agent-sim-external` against the mock first to see the full
   loop. Run `just preflight-live` before any live session; it refuses unless every go-live gate
   passes.

## What not to commit

Credentials, account numbers, captured real API responses that contain account data, statements,
and recorded market data you are not licensed to redistribute. Use synthetic fixtures in tests.
An adapter that can only be tested with a live account is best kept in your own fork.
