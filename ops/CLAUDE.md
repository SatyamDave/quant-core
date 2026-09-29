# ops/ — infrastructure as code, deploys (protected), monitoring

## Owns / does not own
- Owns: infra/ (Terraform), deploy/ (deployment definitions; PROTECTED, root rule 3), monitoring/ (dashboards, alert rules), runtime-security templates.
- Does not own: runbooks (docs/runbooks/), limits (config/limits/), CI workflows (.github/workflows/).

## Commands
- `terraform -chdir=infra fmt -check` and `terraform -chdir=infra validate`
- Alerts with a missing runbook: `grep -rhoE 'docs/runbooks/[A-Za-z0-9._/-]+' monitoring | sort -u | while read f; do test -f "../$f" || echo "missing $f"; done` prints nothing

## MUST
- Infrastructure exists only as code in infra/; nothing is created by hand.
- Deploys are human-triggered and go to a canary before full rollout.
- Every alert rule links to a runbook that exists.
- Trading hosts are separate from research hosts and accept no inbound ports except VPN or bastion.
- Secrets come from the secret manager at runtime; venue keys are trade-only, IP-allowlisted, per strategy and environment, and rotated (root rule 9).
- Audit logs ship off-host.
- The ADR-0040 agentic trading account is separately funded, controlled by the operator, with a hard dollar cap set here, not in `agent/`; the agent process itself never holds a broker credential or `ANTHROPIC_API_KEY` directly — both come from the secret manager into the process that needs them, scoped to that process alone.

## NEVER
- Never commit secrets or state files. Check: `git ls-files | grep -E 'tfstate|\.tfvars$|\.pem$|\.key$'` prints nothing.
- No automatic production deploy from CI.
- Agents never edit deploy/ (denied in .claude/settings.json).

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- docs/runbooks/; docs/runbooks/github-hardening.md; SECURITY.md
- ADR-0040 (agent handoff, agent account funding): docs/adr/0040-agentic-decision.md
