# research/registry

The experiment registry: append-only JSON Lines, one record per trial or model event, failures included (root rule 6).

## Location

- The canonical ledger is `research/registry/log/`: `trials.jsonl` (written by `registry.record`) and `models.jsonl` (written by `ml.registry.append`).
- Every writer and reader takes the path from `registry.REGISTRY_DIR` (and `registry.TRIALS`, `ml.registry.MODELS`). Setting `QC_REGISTRY_DIR` points all of them at another directory.
- `research/tests/test_registry.py` fails if another `trials.jsonl` appears under this directory, if a (experiment, config hash) pair is recorded twice, or if study 0001 has other than exactly one trial.

## What is in the committed ledger

| Experiment | Records | Kind | Source |
|---|---|---|---|
| `demo` | 4 trials in `trials.jsonl`, 1 challenger in `models.jsonl` | **Demo.** Synthetic data from `data/recorders/synthetic.py`, recorded by the first `just walkforward demo` run on 2026-09-27 (report `backtest/reports/demo/2026-09-27.md`). Tests mechanics only; says nothing about any market. | ml stack PR (#12) |
| `study-0001-favorite-longshot` | 1 trial | Real registered study, verdict INSUFFICIENT EVIDENCE (no analysable markets). | `research/studies/0001-favorite-longshot/record_trial.py` |

The demo records stay as history; lines are never edited or deleted. `just walkforward demo` now writes to the gitignored `out/` directory and adds nothing here; only `--record` appends to this ledger. A scratch run still reads the earlier trials for its experiment from this ledger, so its deflated Sharpe counts them plus its own; its report says it is not evidence.

The demo model record predates the `verdict` field, so it has none, and `ml.monitoring.promote` refuses any record whose walk-forward verdict is not PASS. It therefore cannot be promoted, although its status reads `challenger`. Walk-forward runs now register a FAIL as `no_signal`.
