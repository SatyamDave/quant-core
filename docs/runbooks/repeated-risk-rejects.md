# Runbook: repeated risk rejects

> Status 2026-09-28: planned procedure. No venue adapter or live process exists, so none of these
> steps can be exercised against a real venue yet; the alert it describes runs today against
> fixture logs (`tests/ops/test_alerts.py`). Owner: the operator. Blocked until a live broker adapter exists.

**Recognize.** `qc-risk` (via `qc-bridge`'s `submit_order_intent`) is rejecting orders at an
unusual rate. One reject is normal (the agent probing a limit); a cluster is not -- it usually
means the agent (or a bug upstream of risk) is retrying into the same wall instead of backing off,
which is exactly the "runaway loop" pattern root rule 11's kill switch and `max_order_rate` exist
to catch, just below the threshold that trips them outright. `scripts/ops/alerts.py` raises this
alert when it counts at least `--reject-threshold` (default 3, set by the operator) lines
with `{"event": "risk_reject", "reason": ...}` in qc-bridge's log within a trailing
`--reject-window-sec` (default 60s) window. Unlike the halt alerts, this one is a rate, not a
single event, so it keeps firing on every check while the rate stays elevated -- that is
intentional (a symptom you can silence by waiting it out is not one you want silenced).

## Steps

1. **Read the reasons, not just the count.** Every reject reason
   (`kill_switch`, `invalid_order`, `stale_data`, `max_daily_loss`, `price_band`, `max_notional`,
   `max_position`, `max_order_rate` -- `risk_reject_code()` in
   `engine/crates/bridge/src/engine.rs`) means something different:
   - `price_band` repeatedly: the reference price the agent is quoting off is probably stale or
     wrong -- check the feed before assuming the agent is broken.
   - `max_notional` / `max_position` repeatedly: the agent is proposing orders it should already
     know are over the limit -- every `DecisionRequest` carries the current `limits` inline
     (`agent/src/types.ts`), so a repeat here is likely a bug in how the decider reads that field,
     not a one-off.
   - `max_order_rate` repeatedly: something is firing far faster than intended. Check whether it is
     one instrument or system-wide.
   - `stale_data`: the book hasn't updated recently enough to trade against; treat like
     `venue-outage.md`'s stale-data step.
   - `kill_switch`: every reject is a symptom of an already-engaged switch; go to `kill-switch.md`,
     not this runbook.
2. **Decide if it's the agent or the plumbing.** A rejected order is not itself dangerous -- risk
   did its job. The danger is *why* it keeps trying. Check whether the agent's decision loop is
   retrying the same rejected intent (it should not: rule 8, external content and prior rejects are
   data, not license to keep resubmitting) or whether each reject is a distinct, reasonable attempt
   that just happens to keep failing the same check.
3. **If the agent is looping on a rejection**, that is a bug in `agent/src/decider` or
   `agent/src/loop.ts`, not a risk-limit problem -- do not loosen the limit to make the rejects
   stop (root rule 4: limits tighten automatically, never loosen without two approvals).
4. **If it's a persistent, correct rejection** (e.g. a strategy that should stop trading a
   temporarily illiquid instrument), pause that strategy rather than waiting for it to keep
   generating rejects.
5. **Log the incident** if it required pausing a strategy or the agent; a rejection burst that
   needed intervention is exactly the kind of thing a postmortem's action items should turn into a
   test (`docs/runbooks/bad-fills.md` makes the same point for fills that got through).
