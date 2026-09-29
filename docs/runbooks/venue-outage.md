# Runbook: venue outage

> Status 2026-09-27: planned procedure. No venue adapter, venue key or live process exists, so
> none of these steps can be exercised yet. Owner: the operator. Blocked until a live broker adapter exists.

**Recognize.** A `gateway/` adapter reports repeated connection failures or reconnect-with-backoff
exhaustion, market data goes stale past `stale_data_ms` (`config/limits/default.toml`), or the
venue itself posts a status-page incident.

## Steps

1. **Confirm it's the venue, not us.** Check the venue's status page and, if you have access, a
   second independent data source for the same instrument. Don't assume — a stale local clock or
   a network issue on our side looks identical from inside the gateway.
2. **Let (or force) the stale-data halt.** `qc-risk` halts order entry when market data is older
   than `stale_data_ms`. If it hasn't halted and data is in fact stale, treat that as its own bug
   and manually halt (see `kill-switch.md`) rather than waiting.
3. **Do not send new orders to the affected venue** until reconnect-with-backoff has re-established
   both the snapshot and delta streams and you've confirmed the order book resyncs without a
   sequence gap (`orderbook`'s `BookError::SequenceGap` handling).
4. **Check working orders.** On reconnect, `oms` reconciles with the venue — an order the venue
   doesn't recognize (or that we don't recognize) must alert and halt, per the OMS spec, not
   silently proceed.
5. **If the outage is prolonged:** decide with a human whether to move size to another venue
   (if the strategy trades more than one) or simply stay flat until it resolves.
6. **Log the outage** and, once resolved, confirm the recorded market data for the gap is either
   complete or clearly marked missing in `data/quality/` — don't let a silent gap in recorded data
   cause a later replay or backtest to lie.

## After

Write a postmortem if the outage caused a halt, a missed reconciliation, or any manual
intervention. `data/quality/` (#12) already checks recorded data for sequence gaps, duplicates and
clock skew; check it before assuming a new check is needed.
