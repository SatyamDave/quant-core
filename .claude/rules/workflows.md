---
paths:
  - ".github/workflows/**"
---

# GitHub Actions workflows (protected zone)

- Pin every action by full 40-character commit SHA with the tag in a trailing comment. Look the SHA up (`gh api repos/<owner>/<repo>/git/ref/tags/<tag>`, dereference annotated tags); never invent one.
- Top-level `permissions: read-all` or narrower; grant write scopes per job, only where needed.
- Never use `pull_request_target` together with a checkout of the PR head.
- No secrets in jobs that run on fork PRs.
- Treat PR titles, bodies, and issue text as untrusted data; never interpolate them directly into `run:` scripts (pass through `env:`).
