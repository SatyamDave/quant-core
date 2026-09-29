---
paths:
  - "research/**"
  - "ml/**"
---

# Python research and ML

- Point-in-time data only. A feature at time t uses data stamped strictly before t; no lookahead, no future-joined labels.
- Fix every seed (`random`, `numpy`, model libraries) and write the seed into the run config.
- Every backtest or training run, including failures, is appended to the experiment registry (`research/registry`, `registry.record`). Unrecorded runs do not exist (rule 6).
- Validation is purged, embargoed walk-forward; report deflated Sharpe with the true trial count (`registry.trial_count`) and PBO.
- Before proposing an idea, check `docs/research/graveyard.md` and the registry.
- Notebooks are exploration only; nothing under `strategies/` or `engine/` imports from `research/`.
- Commands run from `research/`: `uv run pytest`, `uv run ruff check . ../ml`, `uv run mypy registry ../ml`.
