---
name: pr-ready
description: Get a branch ready for a pull request. Use before opening a PR, when asked to ship, or to check a change is complete.
---
## Steps
1. Run `just check` and fix failures. Never weaken a test or rule to pass.
2. Confirm a test failed before the fix and passes after.
3. Hot-path change: include `just bench` results. Engine change: `just replay` passes.
4. Protected zone touched: request the risk-auditor agent and a human reviewer; say so in the PR body.
5. Research change: link the registry entry.
6. Update docs for changed behavior.
7. Commit with a conventional message (`feat(engine/oms): ...`) and open the PR with `gh pr create`, filling the PR template.
