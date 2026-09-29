---
name: incident-postmortem
description: Write a blameless postmortem. Use after an incident, outage, bad fill, risk breach, kill-switch event, or near miss.
---
## Steps
1. Copy docs/postmortems/0000-template.md to docs/postmortems/YYYY-MM-DD-<slug>.md.
2. Fill in timeline (UTC), impact, root cause, and what the system should have caught.
3. Every action item has an owner and becomes a test, rule, or runbook; link each one.
4. Add a gotcha proposal to .claude/learnings/inbox/ pointing at the postmortem.
5. Never include secrets, keys, or account identifiers.
