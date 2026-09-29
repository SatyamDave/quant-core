# Security policy

## Reporting a vulnerability

Please do not open a public issue, PR or discussion for a vulnerability.

Report it privately through GitHub security advisories: open the repository's **Security** tab
and choose **Report a vulnerability** (or go to
`https://github.com/OWNER/quant-core/security/advisories/new`). Include what is affected, how to
reproduce it, and what access it gives. Maintainers of a fork can turn private reporting on with
`scripts/harden-repo.sh` or in the repository settings.

We aim to acknowledge a report within 3 working days and to agree on a fix and disclosure date
with you. We credit reporters in the advisory unless you ask us not to.

Especially in scope: anything that lets an agent bypass the risk gate or the kill switch, loosen a
limit, reach a broker without an engine-signed approval, or read a credential; and anything that
breaks replay determinism.

If you run quant-core against a real account and suspect a live problem, halt first (kill switch,
`docs/runbooks/kill-switch.md`), then investigate.

## Secret handling

- No secrets in git, ever: not in code, config, tests, fixtures, notebooks, commit messages, issues or PRs. `.env` is for local development only and is gitignored.
- Production and paper secrets live in the secret manager (`ops/deploy/README.md`), scoped per strategy and per environment. Venue keys are trade-only, withdrawal-disabled and IP-allowlisted.
- Never read, print, log or echo a secret, and never paste one into a prompt.
- CI jobs that read PR or other external text do hold a credential: the model API key. The controls around it:
  - Fork PRs get no secrets. `ai-review` runs only for same-repo, non-draft PRs.
  - Every job that calls a paid model first asks the AI gate (`scripts/ci/ai_gate.py`, reading `autonomy/POLICY.yaml`), which ships switched off.
  - The jobs that hold `ANTHROPIC_API_KEY` are `ai-review` (`security-review` and `code-review`: the PR diff, title, body and code, with a `pull-requests: write` token to post comments) and the `propose` jobs of `knowledge-sync`, `gardener` and `learnings-triage` (merged PR diffs, bodies and comments, and the learnings inbox, with a read-only token).
  - None of these jobs gets venue, deploy or secret-manager credentials, or a token that can write code.
- Pre-commit (gitleaks, detect-private-key) and CI (trivy secret scan, semgrep `p/secrets`) look for secrets on every change.
- A leaked secret is rotated immediately, even if the commit never left a laptop. Removing it from history is not a substitute for rotation.

## Merge policy for findings

Scanners: cargo-deny, cargo-audit, pip-audit, bandit, osv-scanner, semgrep, trivy, checkov, CodeQL (when code scanning is available) and the AI reviewers.

| Severity | Rule |
|----------|------|
| Critical, High | The build fails. Fix or upgrade before merge. No ignore entries. |
| Medium | May merge only with (1) a justification comment at the suppression (`# nosec Bxxx: <reason>`, `# checkov:skip=<id>: <reason>`, a `deny.toml` ignore with `reason`) and (2) a linked tracking issue with an owner. |
| Low, Info | Fix when touching the code. |

A suppression without a reason is rejected in review. A tool that could not run is a failed check, not a pass. AI review comments are advisory; they never replace the deterministic gates or human review.

## Supported versions

Only the latest `main` is supported. There are no released versions yet.

## No warranty

quant-core is provided "AS IS" under the Apache-2.0 license. Securing your own deployment,
keys and broker account is your responsibility.
