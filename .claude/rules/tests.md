---
paths:
  - "**/tests/**"
  - "**/*_test.*"
  - "**/test_*.py"
---

# Tests

- A test for a bug fix must fail before the fix. Run it against the unfixed code first and say so in the PR.
- Never weaken an assertion, skip a test, or change expected output to make a check pass. Stop and report instead.
- Tests are deterministic: fixed seeds, `SimClock` instead of wall time, recorded fixtures instead of live venues.
- No live venue connections, real API keys, or testnet orders in any test.
