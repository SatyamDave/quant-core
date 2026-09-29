# Research journal

One entry per ISO week, named `YYYY-Www.md` (for example `2026-W39.md`). The
weekly `learnings-triage` workflow drafts the entry in its `knowledge` PR; a
human reviews and merges it. Entries are records: once merged, they are not
edited, and a correction goes in a later week's entry.

Each entry covers the week's research and ML activity, drawn from the
experiment registry (`research/registry/`), the graveyard
(`docs/research/graveyard.md`) and merged commits under `research/`, `ml/`,
`backtest/` and `strategies/`:

```markdown
# 2026-W39

## Trials
Trials recorded this week and the running total, per strategy or model.

## Results
What passed or failed a gate, with links to reports in backtest/reports/.

## Graveyard
Ideas added this week and the reason each failed.

## Open questions
What the next week should look at, and why.
```

A week with no activity still gets an entry that says so, so a gap in the
journal means the workflow did not run, not that nothing happened.

The journal summarizes; the registry is the source of truth. Numbers here must
match the registry, and trial counts come from it, never from memory.
