# Governance

## Roles

- **Contributors:** anyone who opens an issue, discussion or pull request.
- **Maintainers:** people with merge rights, listed in `.github/CODEOWNERS`. They review PRs,
  triage issues, cut releases and enforce the [Code of Conduct](CODE_OF_CONDUCT.md).
- **Operators:** anyone running their own copy of quant-core. An operator owns their deployment,
  their limits, their broker keys and every order their agent places. The project never has
  access to an operator's account.

## Decisions

- Day-to-day changes are merged by a maintainer after review and green CI.
- Changes that are expensive to reverse (a new dependency, storage format, venue interface or
  schema version) need an ADR in `docs/adr/` and maintainer agreement.
- Changes to a protected zone (see [CONTRIBUTING.md](CONTRIBUTING.md#protected-zones)) need an
  explicit maintainer review. Loosening a default risk limit, or weakening a risk check, the kill
  switch or the replay tests, needs two maintainer approvals and is expected to be rare.
- Maintainers try to reach consensus. When they cannot, the maintainers decide by simple majority.

## Becoming a maintainer

Sustained, high-quality contributions and reviews. An existing maintainer nominates you and the
other maintainers agree. Maintainers who are inactive for six months may be moved to emeritus.

## Safety over features

Any maintainer may block or revert a change that weakens the safety model (risk gate, kill
switch, tighten-only limits, no LLM in the engine, credential isolation) until it is discussed.
