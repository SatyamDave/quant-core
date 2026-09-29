---
name: add-order-type
description: Add a new order type. Use when adding or changing an order type in qc-core OrderType and the OMS state machine.
---
## Steps
1. Add the variant to `OrderType` in engine/crates/core/src/event.rs.
2. Extend the OMS state machine in engine/crates/oms/ with exhaustive matches (no `_ =>` arms).
3. Risk checks must cover the new type; that change is in the protected risk crate and needs human approval and the risk-auditor agent.
4. Add tests for every transition, and replay tests showing identical orders.
5. Run `just check` and `just replay`.
