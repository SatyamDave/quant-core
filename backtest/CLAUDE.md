# backtest/ — fill and latency models, run configs, and reports

## Owns / does not own
- Owns: configs/ (one YAML per run), fill_models/ (queue-position fills for market making), latency_models/ (order entry and market data, calibrated from recorded timestamps), reports/ (`<name>/<date>.md`; `reports/raw/` gitignored).
- Does not own: strategy logic (strategies/), walk-forward statistics (research/, ml/validation), recorded data (data/).

## Commands
- `just backtest <cfg>` (reads `configs/<cfg>.yaml`)
- `just walkforward <name>`
- Reports missing assumptions: `grep -L '^## Assumptions' reports/*/*.md` prints nothing

## MUST
- Every config names fees, funding, latency model, fill model, and slippage; a config missing any of them is refused.
- Market-making fills use a queue-position model, never fill-on-touch.
- Every report starts with an Assumptions section (check above) and links its registry entry.
- Every run is recorded in the experiment registry, failures included (root rule 6).
- A fill or latency model change comes with a calibration note against recorded data and a rerun of affected reports.

## NEVER
- Never commit raw outputs. Check: `git ls-files reports/raw` prints nothing.
- Never change a fill or latency model in the same PR that reports a strategy's results with it.
- Never report net results without fees and funding.
- Never backtest the ADR-0040 agent on history as if it were a strategy: an LLM's pretraining may already contain the outcome. The agent is evaluated forward-only (evals/, docs/adr/0040-agentic-decision.md); this crate's backtests stay classifier- and strategy-only.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- Skills: run-walkforward, overfitting-report (research/.claude/skills/); strategies/_template/GATES.md
- ADR-0040 (agent handoff, forward-only agent evals): docs/adr/0040-agentic-decision.md
