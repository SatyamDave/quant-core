# Runbook: bad fills

> Status 2026-09-27: planned procedure. No venue adapter, venue key or live process exists, so
> none of these steps can be exercised yet. Owner: the operator. Blocked until a live broker adapter exists.

**Recognize.** A fill at a price far from the reference price (should have been rejected by the
`price_band_bps` fat-finger check), a fill that doesn't match an order this system sent, a burst
of fills with markout that's sharply worse than the strategy's expected edge, or a reconciliation
mismatch against the venue statement (root rule 10: PnL is reconciled daily).

## Steps

1. **Stop making it worse.** If fills are still coming in and look wrong, halt the strategy first
   (`kill-switch.md`), investigate second. Don't try to "trade out of it" before you understand
   what happened.
2. **Reconstruct what happened**, in order:
   - the order(s) our OMS believes it sent (`oms` state machine history),
   - what the venue says it received and filled (venue's fill/execution report),
   - what the risk checks should have done (`price_band_bps`, `max_order_rate_per_sec`,
     `max_daily_loss` against the fill's actual notional and cumulative loss).
3. **Identify which layer let it through:** a risk check that should have rejected but didn't, a
   gateway bug (e.g. a stale reference price feeding the price band), an OMS state bug (e.g. an
   order resubmitted after it should have been terminal), or a venue-side issue (a fill the venue
   itself later busts/adjusts).
4. **Reconcile.** Compare the day's fills against the venue's own statement.
   `TODO (fund/track-record phase): reconciliation script does not exist yet — until then, this
   step is manual: pull the venue's fills export and diff by hand.` Any discrepancy blocks closing
   the incident.
5. **Do not edit any existing record to "fix" the number.** Track-record data is append-only (root
   rule 10) — record a correcting entry, never edit history.
6. **Write the postmortem**, including whether a risk limit needs tightening (limits can only be
   tightened automatically, per root rule 4 — loosening always needs two approvals, and a bad-fill
   incident is not, by itself, a reason to loosen anything).

## Prevention checks this should already catch

If a bad fill got through a check that's supposed to prevent it (price band, rate limit, daily
loss), that check's test suite is missing a case — the postmortem's action items should include
a boundary test for exactly this scenario in `engine/crates/risk/`.
