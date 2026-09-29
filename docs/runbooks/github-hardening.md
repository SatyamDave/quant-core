# Runbook: GitHub repository hardening

Owner: repo admin. Run after bootstrap merges, and again whenever a required CI job is renamed.

## Apply

```bash
scripts/harden-repo.sh            # dry run: prints every gh api call
scripts/harden-repo.sh --apply    # needs admin on the repo
```

The script continues past a call that fails and exits non-zero at the end, listing what failed. Re-running is safe: the ruleset is updated in place.

## What it sets

| Setting | How |
|---------|-----|
| PR required to change `main`, 1 approval, code owner review, stale approvals dismissed on new commits, last push must be approved by someone else, threads resolved | ruleset `main-protection`, `pull_request` rule |
| Required checks, branch up to date before merge | `required_status_checks` rule: `rust`, `miri`, `python`, `just-check` (ci.yml); `semgrep`, `trivy`, `checkov`, `osv-scanner / osv-scan` (security.yml); `gate` (autonomy-gate.yml). Each is pinned to the GitHub Actions app. |
| Linear history, squash or rebase merges only | `required_linear_history` rule and repo merge settings |
| No force push, no branch deletion | `non_fast_forward`, `deletion` rules |
| Signed commits | `required_signatures` rule |
| Secret scanning and push protection | `security_and_analysis` |
| Dependabot alerts and security updates | `vulnerability-alerts`, `automated-security-fixes` |
| Private vulnerability reporting | `private-vulnerability-reporting` |

Not required: `codeql` (runs only with code scanning), `ai-review` jobs (advisory, need `ANTHROPIC_API_KEY`), `scorecard`, `fuzz-nightly` (scheduled).

## Plan limits as of 2026-09-27

The repo is private and owned by a user account. Checked with `gh api`:

- Rulesets and branch protection return 403 "Upgrade to GitHub Pro or make this repository public". Nothing in the table above that uses the ruleset takes effect until the account is on GitHub Pro or the repo moves to an organization.
- Code scanning (CodeQL, SARIF upload) and secret scanning with push protection on private repos need GitHub Code Security and Secret Protection, which are sold only on Team and Enterprise plans. The CodeQL job is gated on the repository variable `CODE_SCANNING=true`; set it once code scanning is enabled. Until then gitleaks, trivy and semgrep are the secret and code scanners.
- Private vulnerability reporting is for public repositories only. `SECURITY.md` gives the private-repo route.
- OpenSSF Scorecard cannot publish results for a private repo without Advanced Security; `scorecard.yml` keeps the JSON report as an artifact.

## Manual steps

1. **Second approval for protected paths.** Rulesets can require extra reviewers per file path only for organization teams. For a user-owned repo this is enforced by the `gate` check from `autonomy-gate.yml` (T3 tier: protected zones need two human approvals), which the ruleset makes required. After moving to an organization, also add a `required_reviewers` entry in the ruleset's `pull_request` rule for the protected paths in root `CLAUDE.md`.
2. **Single maintainer.** With one human, `require_last_push_approval` and one required approval mean nobody can merge a PR they pushed. Add a second maintainer before applying, or decide on an admin bypass and record it here.
3. **Signing keys.** Every committer, including any bot that pushes to `main`, needs a verified SSH or GPG signing key before `required_signatures` is on. Squash merges made in the GitHub UI are signed by GitHub.
4. **Actions settings** (Settings > Actions > General): allow only actions pinned by SHA (or "selected actions"), set the default `GITHUB_TOKEN` to read-only, disable "Allow GitHub Actions to create and approve pull requests", and require approval for workflows from outside collaborators.
5. **Secrets.** Add `ANTHROPIC_API_KEY` as a repository secret for `ai-review.yml`. Never add venue keys to GitHub.

## Verify

```bash
gh api repos/SatyamDave/quant-core/rulesets
gh api repos/SatyamDave/quant-core --jq .security_and_analysis
```

Open a test PR that fails one required check and confirm the merge button is blocked.
