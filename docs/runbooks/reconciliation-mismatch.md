# Runbook: reconciliation mismatch

> Status 2026-09-28: planned procedure. No venue adapter or live process exists, and `qc-bridge`
> (`engine/crates/bridge/`) does not do multi-venue reconciliation yet -- that is protocol v1.1's
> `report_execution` op (bridge-v1.1 lane, in flight in parallel), which applies an external
> venue's fills to the OMS and, per that op's own spec, treats an illegal transition as "error and
> halt, as the OMS already does." This runbook is written against that anticipated behavior; the
> alert wiring below (`scripts/ops/alerts.py`) is real and tested today against a fixture log.
> Owner: the operator. Blocked until a live broker adapter exists and bridge-v1.1 landing.

**Recognize.** The bridge's OMS (`qc_oms::Oms`, the same state machine `qc_replay::Engine` uses)
found a discrepancy against the venue's own order state via `report_execution` -- including an
execution event for an order the bridge doesn't recognize, or an illegal state transition for one
it does -- and halted (root rule 10: PnL and positions are reconciled, and any discrepancy is not
something we paper over). `scripts/ops/alerts.py` raises this alert from a `{"event": "halt",
"reason": "reconciliation"}` line in qc-bridge's log (see `ops/live/README.md`); it also fires the
generic **halt** alert (`kill-switch.md`) for the same line, since a reconciliation halt has the
same immediate effect (no new orders, cancels sent).

## Steps

1. **Treat it like a halt first.** New orders are already refused and working orders are already
   being cancelled (`kill-switch.md` covers what a halt does and does not do) -- there is nothing
   to stop before investigating.
2. **Get both sides of the story**, in order:
   - what our OMS believes is open or filled (`qc_oms` state machine history for the affected
     order id(s)),
   - what the venue's own order/execution history says for the same id(s).
3. **Classify the mismatch:**
   - *Unknown venue order*: the venue reports an order we have no record of. Could be a replay of
     an earlier session, a bug that dropped our own record, or (worst case) an order placed
     outside this system on the same account. Do not resume until you know which.
   - *Unknown local order*: we believe an order is open or working that the venue has no record
     of. Do not resubmit it blindly -- confirm first (root protocol note: an ambiguous broker
     response is reconciled before any retry, never resent blindly).
   - *State disagreement*: both sides know the order but disagree on its state (filled vs.
     working, quantity, price). Pull the venue's execution report directly; it is the source of
     truth for what actually happened at the venue.
4. **Do not restart trading** until the mismatch is explained and, if it revealed a bug in `qc_oms`
   or the venue adapter, fixed and covered by a new reconciliation test case.
5. **Reconcile the day's PnL** against the venue statement once the mismatch is understood (root
   rule 10) -- a reconciliation halt during the day is exactly the kind of event that day's
   `fund/track-record` entry should reference.
6. **Write the postmortem** (`docs/postmortems/0000-template.md`). If the root cause was a gap in
   `qc_oms::reconcile`'s coverage, the action item is a new test in `engine/crates/oms`, not a
   process workaround.
