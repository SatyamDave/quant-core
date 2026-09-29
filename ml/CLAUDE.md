# ml/ — planned model pipeline: dataset → features → train → validate → registry → shadow → promote

## Owns / does not own
- Plain Python package run from the research/ uv environment (`import ml`); no pyproject of its own.
- Owns: datasets/ (point-in-time builders, dataset hashes, labels), features/ (the one definition shared with Rust), training/ (challengers), validation/, registry/ (model registry client), monitoring/ (drift, demotion), export/ (artifacts for qc-inference).
- Does not own: the Rust inference runtime (engine/crates/inference), promotion approval (humans), risk limits (config/limits/).

## Commands
- `just test` and `just lint` (pytest, ruff, `mypy --strict` on ml)
- `uv run --project ../research ruff check .`
- `just walkforward <model>`
- Parity: Python features vs `qc-inference` on the same recorded data (test lands with ml/features)

## MUST
- Models stay small and deterministic: linear or GBT, exported to ONNX or hand-coded Rust.
- Datasets are point-in-time from data/catalog and carry a versioned hash; labels are code with tests (for example forward mid return over N ms net of spread).
- Each feature has one definition used by training and inference; the parity test passes on shared recorded data, and a failure blocks merge.
- Training uses fixed seeds and logs the full config to the registry.
- Validation is purged, embargoed walk-forward with deflated Sharpe, PBO, regime stability, turnover and cost sensitivity, against the champion on identical data.
- Registry entries hold dataset hash, feature version, metrics, artifact hash, and lineage.
- The ladder: validation gates → shadow (predictions, no orders) → promotion-dossier → human approval → canary → scaled; each step is recorded in the registry and the strategy README.
- Monitoring demotes a model to "no signal" automatically past its drift or edge threshold.
- Discovery candidates from research/sandbox count toward the trial total.

## NEVER
- Retraining produces a challenger candidate, never authority to deploy or raise risk.
- No online adaptation until simpler offline methods have been tried and measured to fail.
- Never promote automatically. Check: `grep -rniE 'promote' --include='*.py' .` hits only dossier generation, never a registry state change to champion.
- Online adaptation (intraday spread or skew) never leaves the bounds in config/limits/ and never changes a risk limit.
- Never export a model without its registry hash.
- No lookahead: no feature reads data stamped after its decision time.
- Never train or label a model from agent (ADR-0040) decisions or outputs as ground truth; Anthropic's Commercial Terms and Usage Policy bar training on Claude outputs. Labels are realized market outcomes only — the classifier and the agent stay on separate evidence.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/SatyamDave/quant-core/pull/N)) -->

## See also
- Skills: ml/.claude/skills/ (train-challenger, promotion-dossier); research/CLAUDE.md; engine/crates/inference/CLAUDE.md
- ADR-0040 (agent handoff, forward-only agent evals): docs/adr/0040-agentic-decision.md
