## What this does

<!-- One concern per PR. Plain description of the change and why. -->

## Moves us toward the first agentic trade by:

<!-- How this helps an agent trade safely through the risk-wrapped path. Required on every PR (ADR-0040, docs/adr/0040-agentic-decision.md). Non-empty; checked by
     scripts/docs/arch_check.py. -->

## Architecture impact:

<!-- Required, non-empty, if this PR is a major architecture change: a public interface in an
     engine crate, schemas/**, agent/src/**/decider*, docs/adr/**, config/limits/**,
     .github/workflows/**, autonomy/POLICY.yaml, root CLAUDE.md rule text, or a new top-level
     directory. "none" is allowed only when this PR is not a major change; checked by
     scripts/docs/arch_check.py. -->

## Verification

<!-- Exact commands run and their results. "just check" output, not just "it passed." -->

## Checklist

- [ ] For a bug fix: I showed the test failing before the fix, and passing after.
- [ ] Tests were added or updated for the behavior this PR changes.
- [ ] Docs were updated for any changed behavior (nested `CLAUDE.md`, a runbook, the glossary, or
      a strategy README).
- [ ] If this touches a hot-path crate (`engine/crates/{core,orderbook,gateway,oms,
      strategy-runtime,inference}`), a criterion bench was run and any regression >5% is called
      out, not silently accepted.
- [ ] `just replay` passes (or is unaffected — say which).
- [ ] This PR does **not** touch a protected zone (`engine/crates/risk/**`, `config/limits/**`,
      `ops/deploy/**`, `fund/**`, `.github/workflows/**`, `.claude/hooks/**`,
      `.claude/settings.json`, `autonomy/**`) — **or**, if it does, a risk-auditor review or
      explicit human approval has been requested and is noted below, and no numeric limit is
      loosened without two human approvals.
- [ ] No secrets, API keys, account numbers, statements or personal trading data are included
      (in code, fixtures, logs, screenshots or this description).
- [ ] Commits follow Conventional Commits and `just check` passes locally.
- [ ] If this PR reports a research or backtest result, the experiment registry link is included
      below (unrecorded backtests don't exist — root rule 6).

Autonomy tier (reported by autonomy-gate): T0/T1/T2/T3

## Open questions / decisions for a human

<!-- Anything you're not sure about, or that needs explicit sign-off. -->

## Integration notes

<!-- Changes this PR needed in a path it doesn't own, listed here instead of made directly. -->
