# config/go-live: the operator's go-live approval

`scripts/ops/preflight.py` (`just preflight-live`) refuses live trading until `APPROVED.md`
exists in this directory. That gate is `operator_go_live_approval`. This repository does not
commit the file, so a fresh clone always fails closed.

When you have reviewed the checklist below, create `APPROVED.md` with at least one line in this
format:

```
Approved by: <operator name> YYYY-MM-DD
```

Every `Approved by:` line must name an operator and carry a date. A line without either fails the
gate. If more than one person must approve, add one line per person.

Before you approve, check:
- Paper and shadow results reviewed (`just shadow`, `just report-pnl`).
- The instrument's limits are within the canary caps (`config/limits/`, `config/environments/canary.toml`).
- Your broker adapter passes its own tests against a sandbox (`agent/src/broker/README.md`).
- The kill switch has been tested on this host (`touch` the canary `kill_file`).
- The broker keys are trade-only: withdrawals disabled, IPs allowlisted (root `CLAUDE.md` rule 9).

To revoke the approval, delete `APPROVED.md`.
