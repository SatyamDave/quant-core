---
name: learnings-triage
description: Triage session learnings in the inbox. Use when asked to triage, review, or promote learnings, or when `just learnings` shows a backlog.
---
## Steps
1. Run `just learnings` to list .claude/learnings/inbox/.
2. Cluster items by theme. Drop duplicates and noise.
3. For each keeper, write one checkable MUST or NEVER line into the target file's Learned or Gotchas section, with today's date and a source link. Procedures go to a skill; file-type rules go to .claude/rules/.
4. Respect budgets: root CLAUDE.md 150 lines, nested 80, at most 15 Learned entries.
5. Move triaged items to .claude/learnings/archive/.
6. Open one PR labeled `knowledge` touching only markdown, .claude/skills/, and .claude/rules/.
