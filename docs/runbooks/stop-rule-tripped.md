# Runbook: stop rule tripped

> Status 2026-09-28: planned procedure. No live process or venue adapter exists yet
> (`ops/live/README.md`), so this alert has never fired against a real trading day; the stop-rule
> evaluator (`scripts/reports/pnl_report.py`'s `evaluate_stop_rule`) and this alert
> (`scripts/ops/alerts.py`'s `check_stop_rule`) are real and tested today against fixture ledgers
> and a fixture daily P&L record (`tests/evals/test_pnl_report.py`, `tests/ops/test_alerts.py`).
> Owner: the operator. The thresholds this rule checks are **placeholder values the operator must replace** (`scripts/reports/pnl_policy.toml`), not an agreed stop rule -- the operator must
> sign off on the real numbers in writing before
> the first live trade.

**Recognize.** The daily P&L report (`fund/track-record/daily/<date>.json`) for a
trading day has `stop_rule.triggered: true` -- either the agent's cumulative drawdown exceeded
`pnl_policy.toml`'s `max_drawdown_usd`, or the agent has closed `max_consecutive_days_behind_buy_and_hold`
days in a row behind simply holding the same asset. This is the mechanism the stop rule exists for:
"a bad run gets caught by a plan, not by hoping it turns around."

## Steps

1. **Read the record directly**, not just the alert: `fund/track-record/daily/<date>.json`'s
   `stop_rule.reasons` names exactly which condition tripped and by how much; `agent`,
   `buy_and_hold`, and `max_drawdown_usd` in the same file are the numbers behind it.
2. **Confirm the source.** `source` is `"decision_ledger_estimate"` (an assumed fill at the
   decision's own price) or `"reconciled"` (real broker fills). A trip against the
   estimate source is a signal to watch, not yet a broker-confirmed number; treat a `"reconciled"`
   trip as the more load-bearing one.
3. **This alert does not stop trading by itself.** `scripts/reports/pnl_report.py` and
   `scripts/ops/alerts.py` are reporting tools, not an enforcement mechanism (same framing as
   `spend-cap.md` step 2) -- the actual go/no-go decision is a human one, the stop rule must be
   written down and approved by the operator before the first trade happens.
   If trading has not already paused and this trip is real, that is the operator's decision to make
   now, not this runbook's.
4. **Do not loosen `pnl_policy.toml`'s thresholds to make the alert stop.** They are an
   operator decision; loosening them needs the same explicit human approval as any other risk-adjacent limit
   (root CLAUDE.md rule 4, by analogy -- this file sits outside `config/limits/**` only because no
   funded account exists yet to apply a real limit to, per that file's own header comment).
5. **Record the decision.** Whatever the operator decides (stop, continue, adjust exposure) belongs
   in an append-only operator log alongside the reconciliation records this build already
   keeps, not only in chat.
