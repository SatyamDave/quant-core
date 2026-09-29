# engine/crates/bridge — qc-bridge: JSON Lines stdio bridge to an external decision agent

## Owns / does not own
- Owns: protocol v1.2 wire parsing and dispatch (`src/wire.rs`), the engine
  loop (`src/engine.rs`: feed → book → features → signal; risk → OMS →
  `SimVenue`/`report_execution`), approval minting (`src/approval.rs`, order
  + cancel, issue #34/#66), the structured stderr event log
  (`src/eventlog.rs`, issue #64/#44), instrument/trading-hours config
  (`src/instrument.rs`, issue #66), the CLI entry point and `--follow`
  tailing reader (`src/main.rs`, `src/feed.rs`, issue #48/shadow lane).
  Binary `qc-bridge`.
- Does not own: the schemas (`schemas/decision/v1/`, ADR-0040), the TS agent
  service and its `agent/src/broker/` gateway (the approval *verifier* and
  the piece that finishes a real-venue cancel/reconcile), risk limit
  *values* (`config/limits/`, `config/instruments/`), the venue adapter
  (`qc-gateway`), `RiskContext` construction (risk lane).

## Commands
- `cargo test -p qc-bridge --locked`; `cargo clippy -p qc-bridge --all-targets --locked -- -D warnings`
- `cargo deny check` (workspace-wide; required whenever a dependency changes)
- `just bridge` (release binary), `just check`
- `qc-bridge <recording> [--venue sim|external] [--kill-file <path>] [--instrument <path.toml>] ...`
  — no `--kill-file`/`--instrument` is byte-for-byte v1.1 (`schemas/decision/v1/README.md`).
  `--kill-file` also spawns the watcher thread above (issue #44 reopened).
- `--follow [--stop-after-idle-ms <n>]` (issue #48/shadow lane): `<recording>`
  is a file that keeps growing (`src/feed.rs`'s `FollowReader` tails it from
  byte 0, only ever parsing complete lines) instead of one already finished;
  `next_decision_request` blocks (poll every 20ms, never busy-spins) instead
  of reporting the feed exhausted the moment nothing new is visible *yet*.
  `--stop-after-idle-ms` is for tests only — a real run omits it and follows
  forever (`tests/follow.rs`). `BridgeEngine::feed_records` (in
  `src/engine.rs`, additive) is the one hook this needed outside `main.rs`/`feed.rs`.

## MUST
- Every response validates against `schemas/decision/v1/*.schema.json` (`tests/schema_conformance.rs`).
- `submit_order_intent` order: kill switch → reconcile gate (`external`) →
  instrument checks (tick/qty-step/trading-hours) → `qc_risk::RiskEngine` →
  OMS → `SimVenue` or a signed approval; any failed check never reaches the
  OMS/venue (`tests/risk_rejection.rs`, `instrument.rs`, `reconcile.rs`).
- `report_execution` applies a gateway event through `apply_order_event`; an
  illegal transition halts with reason `illegal_order_event`, never applied (`tests/external_mode.rs`).
- Halting (`BridgeEngine::halt`, one-way) always cancels every open order:
  routed to `SimVenue` in `sim`, `PendingCancel` in `external` for the
  gateway to finish (`tests/kill_and_cancel.rs`, `chaos.rs`). In `external`
  mode, `status`/`drain_halt_cancels` (protocol v1.2.1, issue #44's
  cancel-on-halt gap) expose a *fresh, unexpired* signed cancel approval for
  every such order — minted on demand, not stored, tagged
  `"reason":"halt"` in the payload — so the gateway's heartbeat can finish
  the cancel at the real broker without a separate `cancel_order_intent`
  round trip per order (`tests/kill_and_cancel.rs`).
- `--kill-file` is polled on every record *and* op (`process_record`, top of
  `wire::handle_line`), *and* independently by a dedicated watcher thread
  `main.rs` spawns (holding its own clone of the same `Arc<AtomicBool>`
  `KillSwitch`) so it works even if nothing ever calls the bridge again —
  issue #44 reopened: a hung, not merely silent, agent process. The thread
  polls every 50ms and engages the switch directly; `tests/chaos.rs` proves
  the 1-second budget when something keeps polling, and
  `tests/kill_file_watcher.rs` proves the watcher thread alone does it with
  **zero** stdin traffic at all (spawns the real binary, never writes to its
  stdin). That one event's `ts_ns` is a wall-clock read, not the sim/market
  clock (see `main.rs`'s `spawn_kill_file_watcher` doc comment) — the one
  documented exception to the no-wall-clock rule below, since the engine's
  clock is owned solely by the main thread this watcher must not depend on.
- `trading_hours.calendar` (wave3/calendar) optionally names a
  `config/instruments/calendars/*.toml` NYSE holiday/early-close calendar,
  cwd-relative like `limits`. `instrument::TradingHours::is_open` fails a
  holiday closed and an early close at its configured minute, and fails a
  date outside the calendar's own covered years closed too (still
  `outside_trading_hours`) rather than assuming a regular session
  (`tests/instrument.rs`).
- `external` mode refuses `submit_order_intent` until a `reconcile` finds no
  discrepancy (`"unreconciled"`); a discrepancy halts as `reconciliation` (`tests/reconcile.rs`).
- `cancel_order_intent` never checks `self.halted` (risk never blocks a
  cancel); unknown/terminal id is `invalid_cancel`, never a silent no-op.
- No wall clock: time comes only from recorded timestamps via `SimClock`
  (incl. `report_execution.ts_ns`). Two scripted runs give byte-identical
  stdout in `sim` mode (`tests/determinism.rs`); a fresh Ed25519 keypair per
  process means `external`/`hello` is deliberately not held to that bar.
- Stdout is protocol-only; every log (incl. the new structured event log)
  goes to stderr, matching `scripts/ops/alerts.py`'s one vocabulary.
- `ed25519-dalek` stays pinned exact; any bump needs a fresh `cargo deny check`.

## NEVER
- No `HashMap`/`HashSet` (clippy.toml bans them; record order must stay deterministic).
- Never route an order that failed a risk check.
- Never route an order in `external` mode; only `report_execution` may move it forward.
- `jsonschema` stays a dev-dependency, `default-features = false` (in-memory `Registry` only).
- `venue_order_id` (`report_execution`/`reconcile`) is a decimal-integer
  string (UNVERIFIED until #26; one place, `wire::parse_venue_order_id`).
- `instrument::Tz` never grows a hand-rolled DST rule for a zone this repo
  doesn't need — a third zone needs a real tz crate, not another `match` arm.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- `schemas/decision/v1/README.md` (v1.2: `market_ts_ns`, `kill`,
  `cancel_order_intent`, `reconcile`, `--kill-file`, `--instrument`; v1.2.1:
  `drain_halt_cancels`, `pending_cancels`), `docs/adr/0040-*.md`,
  `docs/runbooks/kill-switch.md` (trigger + alerting, measured latency),
  `../replay/src/lib.rs` (`HaltReason` this crate mirrors), `src/approval.rs`
  (also exported as `approval-test-vector.json`), `scripts/ops/alerts.py`
  (the structured-log contract), `agent/src/broker/heartbeat.ts` (the
  gateway-side timer that keeps polling this crate's `status` regardless of
  the decision loop, and drains `pending_cancels` at the real broker).
