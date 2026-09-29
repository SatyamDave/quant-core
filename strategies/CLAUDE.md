# strategies/ — one folder per strategy, each with its lifecycle gate status

## Owns / does not own
- Owns: `_template/` (README, GATES.md, config.yaml, src/, tests/) and one `<strategy_name>/` per strategy: thesis, parameters, strategy code implementing `Strategy` from qc-strategy-runtime, tests, gate history.
- Does not own: risk limits (config/limits/, protected), research code (research/), models (ml/), backtest engines (backtest/), the ADR-0040 agent's decision (`agent/`) — the agent is not a `Strategy` registered here; it is a separate decision source gated by the same qc-risk.

## Commands
- New strategy: use the new-strategy skill (copies `_template/` to `<strategy_name>/`)
- `just backtest <strategy_name>`, `just walkforward <strategy_name>`, `just paper <strategy_name>`
- Blank README fields: `grep -nE '^- \*\*[^*]+:\*\* *$' <strategy_name>/README.md` prints nothing before leaving idea

## MUST
- README holds thesis, edge source, why it persists, capacity estimate, kill criteria, owner, and current gate (check above).
- Gates run in order: idea → research → backtest → walk-forward → paper → canary → scaled (root rule 5). Each advance adds a Gate history row with an evidence link; canary and scaled rows name the approving human.
- Thresholds come from `_template/GATES.md`; a strategy's own GATES.md may be stricter, never looser.
- `config.yaml` `gate:` matches the README's current gate. Check: `grep -H '^gate:' */config.yaml` against `grep -H 'Current gate' */README.md`.
- Every strategy has a replay test on a recorded day in `tests/`.
- A PR moving a strategy to paper, canary, or scaled requests risk-auditor review.

## NEVER
- Never import from research/ (or notebooks). Check: `grep -rnE '^ *(use|import|from) .*(research|notebooks)|path *= *".*research' --include='*.rs' --include='*.py' --include='Cargo.toml' .` prints nothing.
- No risk limits in config.yaml; they live in config/limits/. Check: `grep -nE '^[^#]*(max_|limit)' */config.yaml` prints nothing.
- No credentials or venue keys anywhere here (root rules 2, 9).
- Never skip or back-date a gate row.

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/SatyamDave/quant-core/pull/N)) -->

## See also
- Skill: new-strategy; _template/GATES.md; docs/research/graveyard.md; research/CLAUDE.md
- ADR-0040 (agent handoff): docs/adr/0040-agentic-decision.md
