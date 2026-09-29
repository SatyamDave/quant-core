# research/ — Python research and the shared uv environment for research/ and ml/

## Owns / does not own
- Owns: the single uv project (pyproject.toml, uv.lock, Python 3.12), `registry/` (append-only JSONL experiment registry: `Trial`, `record`, `trial_count`), `features/` (exploratory), `notebooks/`, `sandbox/`, `tests/`.
- Does not own: production features (ml/features), models (ml/), strategy code (strategies/), backtest fill and latency models (backtest/).

## Commands
- `uv sync --locked`
- `uv run pytest`
- `uv run ruff check . ../ml` and `uv run ruff format --check . ../ml`
- `uv run mypy registry ../ml` (strict)
- `just walkforward <name>`; `just check` before pushing

## MUST
- Each study follows docs/process/research-cycle.md: registered before the outcome is measured, with a baseline, manifest and explicit decision.
- Point-in-time data only: every join is as-of the decision timestamp; a lookahead test covers each new feature.
- Validation uses purged, embargoed CV plus walk-forward; no plain k-fold on time series.
- Reports give deflated Sharpe with the true trial count from `trial_count(...)`, and PBO.
- Every trial is written with `registry.record`, failures included (root rule 6). Check: the PR links the registry entries it created.
- Check docs/research/graveyard.md before starting an idea; a failed idea gets a graveyard row.
- Fixed seeds for anything random, logged with the trial.
- Dependencies change only through `uv add` so uv.lock stays in sync (`uv sync --locked` fails otherwise).

## NEVER
- Production code and tests never import notebooks. Check: `grep -rnE '^ *(import|from) +notebooks' --include='*.py' . ../ml ../strategies` prints nothing.
- Never edit or delete registry lines; the registry is append-only.
- No venue credentials or live venue connections from research (root rules 1, 2).
- Never report a Sharpe without its trial count.
- Retrospective LLM forecasts on historical data are exploratory only, never evidence; prospective timestamped predictions are primary.
- The ADR-0040 trading agent is never backtested on history (its pretraining may already contain the outcome); it is evaluated forward-only — recorded shadow decisions on live data, then canary — never as a historical Sharpe.
- Never train or label the classifier from agent (ADR-0040) decisions or outputs; Anthropic's Commercial Terms and Usage Policy bar training on Claude outputs, and it would leak the agent's own bias into "ground truth." Labels come from realized market outcomes only.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/SatyamDave/quant-core/pull/N)) -->

## See also
- Skills: research/.claude/skills/ (run-walkforward, overfitting-report, graveyard-entry)
- Rule: .claude/rules/python-research.md; ADR-0005 (data storage); README.md (layout choice)
- ADR-0040 (agent handoff, forward-only agent evals): docs/adr/0040-agentic-decision.md
