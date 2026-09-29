---
name: write-plan
description: Write a plan before multi-step work. Use when a task touches several files or crates, spans sessions, or needs a human checkpoint.
---
## Steps
1. Create docs/plans/NNNN-<slug>.md (next four-digit number).
2. Write: goal, observable success criteria, steps with the files each touches, how each step is verified (exact `just` commands), risks, and decisions needing a human.
3. List any protected zone the work touches; those steps need human approval.
4. Keep the plan current as work proceeds; it is the source of truth after /compact.
