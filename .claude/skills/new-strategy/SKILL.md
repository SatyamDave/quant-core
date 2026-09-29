---
name: new-strategy
description: Create a new strategy folder from the template. Use when starting, adding, or scaffolding a trading strategy under strategies/.
---
## Steps
1. Check docs/research/graveyard.md and the experiment registry for the idea. If it or a close variant is there, stop and report.
2. Copy `strategies/_template/` to `strategies/<strategy_name>/` (snake_case name).
3. Fill in README.md: thesis, edge source, why it persists, capacity estimate, kill criteria, owner. Set current gate to `idea`.
4. Keep GATES.md thresholds equal to or stricter than the template's. Never loosen them.
5. Edit config.yaml; no secrets, no venue keys.
6. Strategy code must not import from research/.
7. Run `just check`, then open a PR with the pr-ready skill.
