---
paths:
  - "engine/crates/risk/**"
  - "config/limits/**"
  - "autonomy/**"
  - "tests/autonomy/**"
  - "tests/hooks/**"
  - ".github/CODEOWNERS"
  - ".claude/settings*.json"
  - ".claude/agents/risk-auditor.md"
  - "scripts/ci/ai_gate.py"
  - "scripts/knowledge/scope_check.py"
  - "tests/ci/**"
---

# Protected zone: risk, limits, autonomy

These paths are human-approved only (root CLAUDE.md rule 3). `guard-protected.sh` blocks edits unless the session was started with `QC_ALLOW_PROTECTED=1`.

- Never loosen a limit or weaken a check. Loosening needs two human approvals (rule 4); the hook blocks it even with the flag.
- For `max_*`, `price_band_bps`, `stale_data_ms`, larger is looser. For `min_*`, smaller is looser. Removing a key is loosening.
- A new file under `config/limits/` is compared with `default.toml`: it may only tighten. A key that is not in `default.toml`, a non-numeric or non-finite value, or a non-`.toml` file is blocked.
- Limit values are decimal strings or integers parsed to fixed point; never floats.
- Every limit has a test at, just below, and just above its boundary. Add the test in the same change.
- Checks run before every order. No bypass flag may exist in a production build.
- If a change here is needed, propose it in the PR body and request the risk-auditor agent and a human reviewer.
