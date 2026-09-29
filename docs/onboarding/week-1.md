# Week 1

By the end of week 1, you should be able to make a real contribution to at least one area below
without a teammate walking you through it step by step.

## Pick a track

**Engine.** Read all of `engine/crates/{core,orderbook,oms}/`. Understand the order state
machine (`oms`) and why it has no wildcard match arms. Run `just bench` and read one criterion
report. Run `just replay` (expect `replay deterministic`) and read
`engine/.claude/skills/replay-debug/SKILL.md`. Chaos tests: `engine/crates/replay/tests/chaos.rs`.

**Research / ML.** Read `research/README.md` and `ml/CLAUDE.md`. Walk through
`docs/research/graveyard.md` and the experiment registry (`research/registry/`) — every trial,
including failures, is recorded there. Read the skill `research/.claude/skills/run-walkforward/SKILL.md`,
then run `just walkforward demo` (it writes only to gitignored `out/`; `--record` would
write the committed registry) to see what a result has to report: Sharpe, deflated Sharpe using
the *true* trial count, PBO, max drawdown, turnover. On the synthetic demo it ends in Verdict
FAIL, which is expected.

**Agents / tooling.** Read `.claude/` end to end: `settings.json`, `hooks/`, `rules/`, `agents/`,
`skills/`. Try starting a Claude Code session from inside `engine/crates/risk/` and run `/context`
— confirm you see the root `CLAUDE.md`, the `engine` one, the `risk` one, and `risk-guard.md` all
loaded, and nothing you didn't expect.

## Understand the lifecycle gates

Every strategy idea moves through idea → research → backtest → walk-forward → paper → canary →
scaled (`strategies/_template/GATES.md`). Skipping a gate is a blocking error, not a style
preference. Find a strategy (or the template) and trace which gate it's at and what's missing to
advance.

## Understand the knowledge loop

Read `docs/onboarding/how-this-repo-learns.md`. Every nested `CLAUDE.md` "Learned"
section is still an empty comment; the knowledge workflows have never run.

## By Friday

- You've opened at least one PR that isn't a typo fix.
- You know which zones are protected and why, and you haven't tried to route around
  `guard-protected.sh`.
- You know where the experiment registry and graveyard are, and you check them before proposing
  something that sounds new.
- You know the difference between what the engine does today and what's still a `justfile`
  placeholder printing "not implemented yet" — don't assume a recipe exists because it's listed.
