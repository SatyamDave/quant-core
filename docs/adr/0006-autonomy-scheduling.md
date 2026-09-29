# ADR-0006: Run autonomy loops on GitHub Actions schedules with claude-code-action

- Status: Proposed
- Date: 2026-09-27
- Deciders: repo owner (T3: two human approvals once a second maintainer exists)
- Review date: 2027-03-27, or earlier when routines leave research preview

## Context

The autonomy spec (docs/specs/0002-autonomy.md) needs nine recurring loops
(hourly to weekly) that open PRs and issues. It asks us to prefer native Claude
Code scheduling over custom cron if it exists and fits. Each loop must have:

1. a bot identity whose PRs trigger the required checks, from a GitHub App
   installation token with minimal scopes;
2. per-loop max turns, job timeout, daily dollar budget and open-PR cap read
   from `autonomy/POLICY.yaml`;
3. a concurrency group per loop;
4. a global off switch honored in the first step;
5. no venue credentials or venue network access on research, discovery and
   model loops;
6. configuration that is versioned, reviewed and protected in this repo like
   every other control.

Claude Code currently offers three native scheduling options plus the GitHub
Action (checked 2026-09-27):

- **Routines** (cloud): saved prompt plus repos, environment and connectors,
  triggered on a schedule, by API or by GitHub events, run on Anthropic cloud.
  https://code.claude.com/docs/en/routines
- **Desktop scheduled tasks**: run on the local machine while the Desktop app
  is open and the machine is awake.
  https://code.claude.com/docs/en/desktop-scheduled-tasks
- **`/loop` and cron tools**: session-scoped; recurring tasks expire after 7
  days. https://code.claude.com/docs/en/scheduled-tasks
- **Claude Code GitHub Actions** (`anthropics/claude-code-action`): automation
  mode runs any GitHub event including `schedule` when a `prompt` is given.
  https://code.claude.com/docs/en/github-actions and
  https://github.com/anthropics/claude-code-action

## Decision

Use GitHub Actions `schedule` (plus `push`/`workflow_dispatch` where a loop is
event-driven) running `anthropics/claude-code-action` in automation mode, one
workflow per loop, authenticated to GitHub with an installation token from a
custom minimal-scope GitHub App.

Phase B implements this. Budgets are passed through `claude_args`
(`--max-turns`, `--max-budget-usd`), time through `timeout-minutes`, and
concurrency through a `concurrency:` group per loop.

## Why not the native options

Routines are the closest fit, since they run without a machine, but they fail
requirements 1, 2 and 6 today:

- **Identity.** "Routines belong to your individual claude.ai account ...
  commits and pull requests carry your GitHub user"
  (https://code.claude.com/docs/en/routines). A loop PR authored as the owner
  cannot be told apart from human work, the owner's own approval cannot count
  on it, and the scope breaker loses its signal. Requirement 1 asks for a bot
  identity.
- **Budgets.** The page documents an account-wide daily run cap and
  subscription usage, but no per-run turn or dollar cap. "There is no
  permission-mode picker"; runs execute shell commands and connector writes
  "without stopping for approval".
- **Configuration outside the repo.** Prompt, triggers and environment live at
  claude.ai/code/routines, not in a reviewable file, so CODEOWNERS and the
  autonomy gate cannot protect them.
- **Maturity.** "Routines are in research preview. Behavior, limits, and the
  API surface may change." GitHub webhook events beyond hourly caps "are
  dropped".

What routines do cover: the 1-hour minimum interval suits `loop-health`, and
cloud environments support a custom network allowlist
(https://code.claude.com/docs/en/cloud-environments), which would meet
requirement 5.

Desktop scheduled tasks skip runs while the machine sleeps and run with local
files and credentials in reach. `/loop` expires after 7 days and needs an open
session. Neither suits unattended loops.

## Why GitHub Actions fits

- Workflows are files in `.github/workflows/`, which is a protected zone, so
  every loop change is a T3 PR.
- `claude_args` accepts CLI flags (action.yml: "Additional arguments to pass
  directly to Claude CLI"). The CLI reference documents `--max-turns` and
  `--max-budget-usd` for print mode
  (https://code.claude.com/docs/en/cli-reference). The GitHub Actions page
  advises "Set `--max-turns` in `claude_args`" and "workflow-level timeouts".
- `github_token` accepts a custom app token. The docs warn that "GitHub doesn't
  trigger workflows on commits made with the default `GITHUB_TOKEN`" and point
  to a custom GitHub App with Contents, Issues and Pull requests only.
- Runner choice isolates research, discovery and models from venue credentials
  and venue network, with no venue secret defined for those jobs.
- `concurrency:` and `timeout-minutes` are native.

## Consequences

- Needs an `ANTHROPIC_API_KEY` secret or workload identity federation (OIDC,
  `id-token: write`; see the GitHub Actions page). Federation is preferred
  because it stores no long-lived key. Phase B decides.
- The action rejects bot actors unless they are listed in `allowed_bots`, and
  scheduled runs are attributed to whoever last edited the cron. If that
  person is a bot, it must be listed in `allowed_bots` (GitHub Actions page,
  "Who can trigger runs").
- GitHub runs schedules only from the default branch and may start them late
  under load. Loops must be idempotent and must not assume exact start times.
- Dollar spend is metered per run by `--max-budget-usd`. Daily and monthly
  caps across runs need the Phase D outcome log and the Phase H cost breaker.
  Phase B must also confirm that the flag is honored when passed through the
  action.
- Pin for Phase B, looked up 2026-09-27: `anthropics/claude-code-action`
  v1.0.235 is the annotated tag `f33305702e43b9f71a532e6f80aed9a399df8288`,
  which points to commit `756cc22e19660d20e8cc9496b4f242475a7f7790`. Pin the
  commit, and look it up again when Phase B starts.

## Alternatives considered

| Option | Verdict |
|---|---|
| Routines (cloud) | Rejected for now: loop PRs would carry the owner's GitHub identity, there is no per-run budget, config lives outside the repo, and the feature is in research preview. Revisit at the review date. |
| Desktop scheduled tasks | Rejected: runs are skipped while the machine sleeps, and local credentials are in reach. |
| `/loop` / cron tools | Rejected: tied to one session, 7-day expiry. |
| Self-hosted cron + headless `claude -p` / Agent SDK (https://code.claude.com/docs/en/headless) | Rejected: more infrastructure to secure than Actions, for no capability Actions lacks. The Agent SDK stays the option for `just evals` (Phase E). |
