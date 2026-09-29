# Runbook: kill switch

Status 2026-09-28: nothing trades, so this runbook describes what the kill switch does in the
simulated engine (#15) and what a live procedure still lacks. Owner: the operator.

## What exists

`qc_risk::KillSwitch` (`engine/crates/risk/src/lib.rs`) is a shared `Arc<AtomicBool>`. Any thread
holding a clone can call `engage()`. There is no reset; clearing it means restarting the process.

When it is engaged, `qc_replay::Engine` (`engine/crates/replay/src/lib.rs`):

1. Notices on the next record it processes (`check_kill_switch`) or when risk rejects an order
   with `RiskReject::KillSwitch`, and records `halt KillSwitch` in the order log.
2. Refuses every new order: risk rejects them, and the engine drops any command after the halt.
3. Sends a cancel to the venue for every working order that is not already pending cancel, and
   sends again, on every later record, any cancel the venue could not take (for example while
   disconnected), until no order is open.

What it does not do:

- It does not confirm cancels beyond the venue's own answers. The OMS keeps each order in
  `PendingCancel` until the venue answers. SimVenue answers every cancel while connected, so the
  chaos test ends with 0 open orders; a real venue could still lose or refuse one.
- It does not flatten or hedge positions. Inventory stays as it was.
- It is not engaged by anything outside the process: there is no operator command, endpoint or
  file flag.
- It is checked per record, so the one-second bound (root rule 11) depends on records arriving.
  The chaos test `kill_switch_from_another_thread_halts_within_one_second`
  (`engine/crates/replay/tests/chaos.rs`) engages it from another thread and asserts the halt
  within 1 s and no submission after it; it runs in `cargo test` and in CI on `main`, but no check is
  required (branch protection is unavailable on this plan).

Other halt reasons with the same effect (no new orders, cancels): `MaxDailyLoss` (realized plus
unrealized loss reached the limit), `IllegalOrderEvent`,
`Reconciliation` (any discrepancy, including an unknown venue order) and `Runaway`.

## qc-bridge: an externally-triggered switch that works even if the agent is stuck (#44)

`qc_replay::Engine` above has no trigger outside the process. `qc-bridge` (`engine/crates/bridge/`,
the live/agent-facing engine, ADR-0040) does, as of protocol v1.2, and this is the manual procedure
for tripping it:

1. **File trigger (works even if the agent process is dead or hung).** Start `qc-bridge` with
   `--kill-file <path>`, then from an operator shell: `touch <path>` (contents are never read, only
   existence). Two independent mechanisms now observe it, so an operator's `touch` is never
   relying on just one:
   - The bridge itself polls this path on every record it processes and every op it dispatches
     (`wire::handle_line`, `engine.rs`'s `process_record`) — whatever is still alive and sending
     the bridge *anything* discovers it immediately.
   - **A dedicated watcher thread** (`engine/crates/bridge/src/main.rs`, spawned whenever
     `--kill-file` is given) polls the same path directly, independent of stdin entirely, holding
     its own clone of the same `Arc<AtomicBool>` `KillSwitch` (confirmed: `qc_risk::KillSwitch` is
     exactly that, and already `Clone`). It engages the switch itself the moment the file appears
     — no op, no record, no cooperation from the agent process required at all. This closes the
     reopened half of issue #44: "a hung (alive but stuck) agent loop never calls it."
     **Measured latency** (`tests/kill_file_watcher.rs`, spawns the real released binary, writes
     zero bytes to its stdin, ever): 12–36 ms across five runs on a development machine, from the
     file's `fs::write` to the watcher's own `kill_switch_engaged` line appearing on stderr — well
     inside the 250 ms this thread's own polling interval (50 ms) budgets for, and inside root
     CLAUDE.md rule 11's one-second bound with wide margin.
   - `tests/chaos.rs` (`kill_file_halts_within_one_second_and_cancels_every_open_order`) is the
     older proof, still valid, that the bound holds even by the polling-only path (something
     keeps calling the bridge during the outage).
2. **In-band trigger.** If something can still send the bridge a line on its stdin (the gateway, a
   manual `qc-bridge` session), send `{"v":1,"op":"kill","reason":"<why>","id":"<any>"}`. Same
   effect, immediate instead of polled.
3. Both engage the same shared `KillSwitch` a plain external `engage()` call would (root rule 11),
   and both then run the same one-way `halt`: refuse every new `submit_order_intent`, and cancel
   every open order (`SimVenue` in `sim` mode; `PendingCancel` in `external` mode).
4. **The gateway's own heartbeat is what finishes the job end to end (issue #44 reopened, second
   half).** `agent/src/broker/heartbeat.ts`'s `Heartbeat` runs a real `setInterval` (default 1s),
   started by `cli.ts` alongside the loop and **decoupled from it** — it keeps calling the bridge's
   `status` op even while the decision loop itself is stuck awaiting a decider that never returns
   (a live LLM call that hangs is the case this exists for). Once a tick observes `halted`, it
   reads `status.pending_cancels` — a fresh, unexpired, Ed25519-signed cancel `approval` for every
   order the bridge moved to `PendingCancel`, tagged `"reason":"halt"` in the payload (protocol
   v1.2.1) — and calls `BrokerGateway.cancelOrder` for each, retrying every tick (the approval is
   re-minted, never stale, so this is safe to call repeatedly) until nothing is left open at the
   real broker. **Measured latency** (`agent/tests/heartbeat-halt-cancel.test.ts`, real binary +
   mock broker, 25 ms heartbeat interval for a fast test): 27–29 ms from the kill file appearing to
   the heartbeat observing `halted`, across three runs; the same suite proves four resting orders
   at the mock broker are all cancelled — exactly one cancel call each — within a 5 s timeout, and
   that the resulting ledger (signature-redacted) and broker journal (session-redacted) hash identically across two
   independent runs. Before this PR, nothing played this role: `agent/src/loop.ts`'s reconcile
   calls were decision-count-based (tied to loop progress), `ops/live/supervisor.py` only reacts to
   process *exit*, and no code path minted a cancel approval for a halt-triggered `PendingCancel`
   order at all (only an explicit, per-order `cancel_order_intent` call did) — so a real deployment
   could observe the halt but never actually get the resting order cancelled at the broker.
5. Everything after tripping the switch is the same as the plain procedure below: confirm, decide on
   positions, notify, don't restart blind, write it up.

## Procedure for a live process (planned, not available)

Blocked on a venue adapter and an operator trigger.
Until they exist, the steps below are the intended shape, not a working procedure.

1. **Trip the switch.** Planned: an operator command that engages `KillSwitch` in every engine.
   Without it, stop the process, which also stops the cancel requests from being sent.
2. **Confirm no new orders** in the venue's own order history, not only this repo's logs.
3. **Confirm cancels** in the venue's open-orders view. Cancel anything still working by hand.
4. **Decide on positions.** The switch never flattens; a human decides whether and how to reduce
   inventory, and records the choice and reason.
5. **Notify** by opening an issue from `.github/ISSUE_TEMPLATE/incident.yml`: what tripped it,
   when, what is still open.
6. **Do not restart** until the cause is understood and, if it touched a risk limit or protected
   zone, a human has approved resuming.
7. **Write it up** with `docs/postmortems/0000-template.md`. Every action item becomes a test, a
   rule or a runbook change.

## Alerting (#64)

`qc-bridge` (`engine/crates/bridge/src/eventlog.rs`, protocol v1.2) now writes one JSON object per
line to stderr on every halt, kill-switch engagement, risk reject, reconcile result and accepted
intent, matching `scripts/ops/alerts.py`'s contract exactly (one snake_case vocabulary, not two):
`{"ts_ns", "event": "halt", "reason": "kill_switch"|"max_daily_loss"|"reconciliation"|
"illegal_order_event", "detail"?}` and `{"ts_ns", "event": "kill_switch_engaged", "source":
"file"|"op"}` for the externally-triggered switch above (fires before the bridge's next message
would otherwise observe the halt). `alerts.py` raises a generic **halt** alert for any `event:
"halt"` line, and a narrower **kill_switch** alert specifically for `reason == "kill_switch"` or a
`kill_switch_engaged` line; both fire from the same trigger. See `ops/live/README.md` for where the
log file lives and how the alert checker runs, `scripts/ops/alerts.py`'s module docstring for the
full contract, and `tests/ops/test_bridge_alerts_contract.py` for the real-binary proof that the
bridge's own stderr output actually drives `alerts.py`'s rules end to end.

One documented exception: a `kill_switch_engaged` line logged by the watcher thread above
(`source: "file"`, detected with zero stdin traffic) uses a real wall-clock read for `ts_ns`, not
the bridge's sim/market clock every other log line uses — that clock lives inside `BridgeEngine`,
owned solely by the main thread, which is exactly what this thread must not wait on. Every other
event this crate logs, including a `kill_switch_engaged` line the main thread itself logs first
(the ordinary, polling-only path), still uses market time.

`scripts/ops/alerts.py`'s contract is otherwise unchanged by protocol v1.2.1: a halt-triggered
cancel approval (`status`/`drain_halt_cancels`'s `pending_cancels`) is not itself a new log event —
it is silent unless the cancel later fails, in which case the gateway's own
`report_execution`/`cancel_rejected` path is what surfaces it.
