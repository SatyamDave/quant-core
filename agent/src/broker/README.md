# agent/src/broker: the order door and the broker interface

This directory executes only orders `qc-bridge` has already approved. It is never registered as an
agent tool, and nothing under `src/decider/` can import it (`tests/broker/no-decider-import.test.ts`).

| File | Role |
|---|---|
| `adapter.ts` | `BrokerAdapter`, the generic broker interface, plus the shared helpers (`refIdForApproval`, `bridgeVenueOrderId`, `orderRefusal`, `loadBrokerAdapter`). |
| `gateway.ts` | `BrokerGateway`: verifies each Ed25519 approval, checks it field by field, rejects expired ones, journals before sending, looks up an unknown outcome before any resend, emulates IOC. |
| `reconcile.ts` | Pulls orders, position and cash from the adapter and hands them to the bridge's `reconcile` op. |
| `heartbeat.ts` | Polls bridge `status` on its own timer and finishes halt-triggered cancels. |
| `journal.ts` | The fsync'd, append-only broker journal. |
| `external-mode.ts` | Wires the pieces above together for `venue_mode: "external"`. |

No real broker adapter ships with this repository. The synthetic mock broker
(`src/testkit/mock-broker.ts`) is the reference implementation. The tests,
`just agent-sim-external` and `just record-quotes --dry-run` all run against it.

## Writing a broker adapter

1. Create a module outside `src/decider/`, for example `adapters/my-broker.ts`, that implements
   every method of `BrokerAdapter` and default-exports an async factory:

   ```ts
   import type { BrokerAdapter } from "../src/broker/adapter.js";
   export default async function create(): Promise<BrokerAdapter> {
     return new MyBrokerAdapter(/* read your credentials here, from your secret manager */);
   }
   ```

2. Keep these rules. The gateway's safety depends on them:
   - **Fail closed.** Throw on any response you cannot parse. Never turn a strange response into a
     default value.
   - **Idempotency.** Send `refId` as the venue's client order id or idempotency key, so that a
     resent order is deduplicated. `findOrderByRefId` must find an order by that key.
   - **Limit orders only.** The gateway sends whole-share limit orders (`orderRefusal`). Map
     `time_in_force: "ioc"` to your venue's day order: the gateway emulates IOC by cancelling the
     order itself.
   - **Honour `opts.timeoutMs`.** Reject when it passes. The gateway treats a rejected call as
     "outcome unknown".
   - **Report the venue's state.** `getOrderById`, `listOrders` and `getAccountState` return what
     the venue says. They never return what you expect it to say.
   - **Keep account ids and credentials inside the adapter.** They must never appear in errors,
     logs or `raw`, because errors are logged and written to the reconcile status file.
   - **Keep keys trade-only.** Disable withdrawals, allowlist IPs, and scope keys per strategy and
     environment (root `CLAUDE.md` rule 9).
3. Point the agent at the module: `QC_BROKER_MODULE=adapters/my-broker.ts`. When the bridge
   reports `venue_mode: "external"`, `src/cli.ts` loads it. Without it, the agent refuses to start.
4. Copy `tests/broker/gateway.test.ts` and point it at your adapter's sandbox or paper
   environment, then run `scripts/ops/preflight.py` before any live run.
