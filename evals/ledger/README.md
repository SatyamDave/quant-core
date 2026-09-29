# evals/ledger

Where `scripts/eval/live_agent.py` (Tier 2, gated, paid) appends its results, one JSON object per
line, to `agent-evals.jsonl`. That file is gitignored (see root `.gitignore`): CI uploads it as a
workflow artifact (`out/eval` upload in `.github/workflows/eval.yml`), it is never committed by the
workflow, and it never holds a real `ANTHROPIC_API_KEY` or other secret — only scenario ids,
decisions and outcome metrics. A human decides if a run is worth keeping and commits it themselves
(root CLAUDE.md rule 6: every experiment recorded, including failures; "unrecorded backtests don't
exist" — recording an *agent* eval this way is a deliberate human act, not an automatic one, per
the eval-cadence design in `llm-trading-eval-research.md` §5).

Each line: `{"ts", "scenario", "model", "prompt_version", "decision", "outcome_metrics", "gate_reason"}`
at minimum; `scripts/eval/live_agent.py`'s docstring is the source of truth for the exact shape.
