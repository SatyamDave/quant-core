# Runbook: daily AI spend approaching cap

> Status 2026-09-28: planned procedure. No live process or venue adapter exists yet, so this
> alert has never fired against a real day's spend; the agent service and its ledger
> (`out/agent/ledger.jsonl`, `agent/src/ledger.ts`, `agent/src/types.ts`'s
> `DecisionLedgerEntry.cost_usd`) are real, and the alert runs today against fixture ledger files
> (`tests/ops/test_alerts.py`). Owner: the operator. Blocked on the operator setting a real daily
> cap (separate from the trading dollar caps: this is the AI-model call budget).

**Recognize.** The agent's cumulative spend on real (non-fake) model calls for the current UTC day
has crossed 80% of the configured daily cap. This is not itself an incident -- it is a warning that
trading will stop for the day soon (or already has, if the cap is enforced elsewhere) and gives a
human time to decide whether that is expected before it happens. `scripts/ops/alerts.py` sums the
optional `cost_usd` field across ledger entries whose (nested) `request.ts_ns` falls in the
current UTC day, and alerts when that sum exceeds `--spend-ratio` (default 0.8) times
`--daily-cap-usd`. `cost_usd` is a decimal string (`agent/src/types.ts`, wave 2 -- it was a JSON
number through wave 1). **The default cap in this repo is a small placeholder pending
operator placeholder, not an agreed number** -- see `ops/live/README.md` and
`scripts/ops/alerts.py --help`.

## Steps

1. **Confirm the number is real**, not a bug: check the ledger file directly for the day's entries
   and their `cost_usd` values; a sudden jump usually means either a burst of real calls (expected
   near the cap) or a single call that mis-recorded its cost (a bug in the agent service, not a
   spend problem).
2. **Confirm the enforcement side matches the alert side.** This alert is an early-warning signal,
   not the enforcement mechanism -- the actual stop-trading-at-cap behavior belongs to whatever
   reads `autonomy/POLICY.yaml`'s `loop-agent-eval` budget or a live-trading-specific
   cap, not to `scripts/ops/alerts.py`. If spend is past the cap and trading has *not* stopped,
   that is a more urgent problem than the alert itself -- treat it like a missing risk check
   (`bad-fills.md`'s framing) and halt manually (`kill-switch.md`) if needed.
3. **If the pace is expected** (e.g. a deliberate live eval run), no action beyond noting
   it is needed.
4. **If the pace is not expected**, find what changed: a prompt change that increased tokens per
   call, a retry loop calling the model more than once per decision, or simply more decisions than
   planned in the window.
5. **Do not raise the cap to make the alert stop.** The cap is an operator decision (`config/limits/` for
   trading dollar caps, the `loop-agent-eval` entry for AI-model spend); a
   change to either needs the same two-approval process as any other limit (root rule 4 by
   analogy, and `autonomy/**` is T3/two-approval regardless).
