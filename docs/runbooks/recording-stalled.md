# Runbook: price recording stalled during market hours

> Status 2026-09-28: planned procedure. The check (`scripts/ops/alerts.py`'s
> `check_recording_stalled`) is real and tested against fixture files and the real
> `config/instruments/spy.toml` calendar (`tests/ops/test_alerts.py`); no live run has fired it
> yet. Owner: the operator.

**Recognize.** `qc-bridge --follow` waits indefinitely for the recording to grow (PR #98), so a
recorder that stops writing looks exactly like a quiet market: no decisions, no errors. This
alert fires when the recording file (`data/raw/shadow/<date>.csv` for `just shadow`, or the file
named first in `QC_BRIDGE_ARGS` on the live host) has not been written for more than
`--stall-after-sec` (default 120 s) while the instrument's market is open per its
`[trading_hours]` and `config/instruments/calendars/nyse.toml`. A missing file during open hours
also fires. It never fires on weekends, holidays or after an early close.

## Steps

1. **Check the recorder is alive.** For `just shadow`, read `out/shadow/<date>/heartbeat.jsonl`
   (`alive.recorder`) and `out/shadow/<date>/recorder.log`; on the live host, the recorder's own
   log under `ops/live/logs/`.
2. **Read the recorder's last error.** The usual causes are an expired broker session
   (re-authenticate your broker adapter, see `ops/live/README.md`), a network outage
   (`venue-outage.md`), or a crash.
3. **Do not trade on a stale book.** While the recording is stalled the agent sees no new
   prices. If a position is open and the recorder cannot be restored quickly, halt
   (`kill-switch.md`).
4. **Restart the recorder** onto the same file; qc-bridge picks up the new rows. Confirm the
   alert stops on the next run.
5. **If the market really was closed** (a closure missing from the calendar), add it to
   `config/instruments/calendars/nyse.toml` in a PR; that file is also what qc-bridge reads.
