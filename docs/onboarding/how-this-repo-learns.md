# How this repo learns

Nothing here self-modifies silently. Every change to instructions, rules, or skills is a reviewed
PR with a traceable source. This is the designed loop (bootstrap spec §8). Status on 2026-09-28:
the Stop hook is on `main` and tested (#11); the workflows are on `main` (#10) but have never run:
`scripts/ci/ai_gate.py` denies them under `autonomy/POLICY.yaml`, no `ANTHROPIC_API_KEY` exists,
and they open PRs with `GITHUB_TOKEN`, which triggers no checks. Nothing below is live.

```
Claude Code session
      │  Stop hook: capture-learnings.py reads the transcript, looks for human
      │  corrections, repeated failures, new gotchas
      ▼
.claude/learnings/inbox/YYYY-MM-DD-<slug>.md
      │  (what happened, proposed rule, target file, evidence — secrets stripped)
      │
      ├──► on merge to main: knowledge-sync.yml reads the merged PR's diff, title,
      │    description, and review comments, plus relevant inbox entries
      │
      └──► weekly: learnings-triage.yml clusters the inbox, promotes repeats into
           rules/skills, drops noise, archives to .claude/learnings/archive/
                    │
                    ▼
           docs-gardener proposes edits: nested CLAUDE.md "Learned"/"Gotchas"
           sections, docs/ARCHITECTURE.md (if structure changed), GLOSSARY.md,
           or a skill (if a procedure changed)
                    │
                    ▼
           ONE pull request, labeled `knowledge`
                    │
                    ▼
           knowledge-scope-check.yml: fails the PR if it touches anything other
           than *.md / .claude/skills/** / .claude/rules/**, if a CLAUDE.md
           exceeds its line budget, or if a Learned entry has no date and source
                    │
                    ▼
           human review and merge
```

Monthly, `gardener.yml` prunes stale "Learned" entries, flags instructions that reference deleted
paths, finds skills nobody's used, and checks that every alert-linked runbook still exists.

## Why it's shaped this way

- **The Stop hook only proposes.** It writes to an inbox file, never edits a `CLAUDE.md` or a
  rule directly — so a bad or half-formed observation from one session can't quietly change
  behavior for every future session.
- **`knowledge-scope-check.yml` is the actual guarantee.** Anyone (including an agent) could claim
  a PR is "just docs." The scope check makes that claim mechanically true or fails the PR.
- **Every "Learned" entry cites a source** (a PR or postmortem link) and a date, so six months from
  now you can tell whether a rule is still relevant to the code as it exists, not just trust it.
- **This is why you never hand-edit a nested `CLAUDE.md` outside a `knowledge`-labeled PR** (see
  `CONTRIBUTING.md`) unless a human explicitly asks — doing so bypasses the one place this loop is
  reviewed.

## Autonomy, decision layer, and model learning

- **Autonomy tiers** (#3): `autonomy/POLICY.yaml` and `autonomy/classify.py` classify a PR into
  T0-T3 and the `autonomy-gate` check reports the approvals it needs. No loop exists, nothing
  auto-merges; the AI workflows read `POLICY.yaml` through `scripts/ci/ai_gate.py` and stay off.
- **Decision layer** (#4): ADRs only. No code; distillation blocked by the vendor contract.
- **Model learning** (#12): an offline pipeline (datasets, training, walk-forward, registry,
  monitoring) exercised on synthetic data. Nothing retrains on a schedule. L0-L3 (#2) is an ADR.
- **Research memory:** the JSONL registry, `docs/research/graveyard.md` and study reports
  (`research/studies/`) are the only learning that has actually accumulated.
