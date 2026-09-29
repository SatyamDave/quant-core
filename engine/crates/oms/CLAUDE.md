# engine/crates/oms — qc-oms: order state machine and venue reconciliation

## Owns / does not own
- Owns: `OrderState` and its transitions, the engine's belief about every open order, reconciliation with the venue at startup and periodically, alerting and halting on unknown orders.
- Does not own: whether an order may be sent (qc-risk), how it is sent (qc-gateway), what to send (strategies or, via `qc-bridge`, the agent service; `agent/`, ADR-0040).

## Commands
- `cargo test -p qc-oms --locked`
- `cargo clippy -p qc-oms --all-targets --locked -- -D warnings`
- `just replay` after any transition change

## MUST
- Every `match` on `OrderState` lists every variant. Check: `grep -nE '^\s*_ *=>' src/*.rs` prints nothing.
- Every transition, legal and illegal, has a test; an illegal transition is an error, not a silent no-op.
- Terminal states (`Filled`, `Canceled`, `Rejected`) never change again (tested).
- Reconcile open orders with the venue on startup before the first new order, and on a fixed interval after. No order goes out until a reconcile has succeeded, and none after a failed one until the next succeeds. Positions are not reconciled yet: the adapter has no position endpoint, so only each order's fills are compared.
- An order the venue reports that the OMS does not know raises an alert and halts the strategy.
- A new variant or transition change needs risk-auditor review (see .claude/agents/risk-auditor.md).
- An intent that reached this crate came from `qc-bridge` after `qc-risk` accepted it (ADR-0040); OMS runs the same state machine for it as for any other order, with no agent-specific case.

## NEVER
- No wildcard `_` arms on order enums (check above).
- Never guess an order's state after a timeout; it stays pending until the venue or reconciliation says otherwise.
- Never auto-cancel or auto-adopt an unknown order; halt and alert.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/SatyamDave/quant-core/pull/N)) -->

## See also
- Skill: add-order-type (engine/.claude/skills/), ../../CLAUDE.md, tests/integration/
- ADR-0040 (agent handoff): docs/adr/0040-agentic-decision.md
