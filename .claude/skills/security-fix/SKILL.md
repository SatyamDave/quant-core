---
name: security-fix
description: Fix a security finding. Use for a vulnerability report, failed audit (cargo deny, cargo audit, pip-audit), leaked secret, or security scanner alert.
---
## Steps
1. If a secret leaked: stop, tell the human to rotate it now. Do not print it. Rewriting history needs the human.
2. Reproduce the finding (`just audit` or the scanner named in the alert) and record the tool output without secret values.
3. Make the smallest fix: upgrade or replace the dependency, or fix the code path. Never add an ignore without a justification comment and a tracking issue.
4. Add a regression test or scanner rule that fails without the fix.
5. Run `just check` and open a PR; request the security-auditor agent.
