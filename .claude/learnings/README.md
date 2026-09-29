# Session learnings

The Stop hook (`.claude/hooks/capture-learnings.py`) reads each session transcript and, when it finds a human correction, a tool failing repeatedly, or a gotcha Claude stated, writes one proposal per session to `inbox/YYYY-MM-DD-<session>.md`. Secret-looking strings are redacted before writing.

A proposal has four sections: what happened, a draft rule, the target file, and the evidence. The hook never edits CLAUDE.md or any rule; it only writes to `inbox/`.

- `just learnings` counts and lists untriaged proposals.
- Triage (the `learnings-triage` skill, or the weekly workflow) turns keepers into dated, sourced entries in a nested CLAUDE.md, a skill, or a rule, through a PR labeled `knowledge`, and moves the proposal to `archive/`.
- Delete a proposal that is noise; do not edit it into something it was not.
