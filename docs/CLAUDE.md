# docs/ — vision, architecture, ADRs, plans, runbooks, postmortems, research graveyard

## Owns / does not own
- Owns: VISION.md, ARCHITECTURE.md, GLOSSARY.md, adr/, specs/, strategies/, research/ (graveyard, journal), runbooks/, postmortems/, onboarding/.
- Does not own: per-directory conventions (nested CLAUDE.md files), procedures (.claude/skills/), strategy READMEs (strategies/).

## Commands
- New ADR: copy `adr/0000-template.md` to `adr/NNNN-<slug>.md` with the next free number: `ls adr | tail -1`.
- New postmortem: copy `postmortems/0000-template.md` to `postmortems/YYYY-MM-DD-<slug>.md`.
- Broken internal links: `grep -rnoE '\]\([^)#]+\)' . | grep -v http` and confirm each target exists.

## MUST
- ADRs have Status, Context, Decision, Alternatives, Consequences, Review date. Check: `grep -L '^## Review date' adr/[0-9]*.md` prints nothing.
- A superseded ADR stays in place with `Status: Superseded by ADR-NNNN`. Check: `git log --diff-filter=D -- adr/` is empty.
- Every postmortem action item has an owner and becomes a test, rule, or runbook, linked from the item.
- Every alert in ops/monitoring links to a runbook in runbooks/ that exists.
- A graveyard entry has a date, the gate reached, why it failed, and a registry link.
- ARCHITECTURE.md changes in the same PR as any structural change (new crate, new top-level directory, new data path).
- Amending a root CLAUDE.md non-negotiable rule gets its own ADR, referenced from the root file. ADR-0040 (agent handoff, superseding the old rule 1) is the precedent.

## NEVER
- Never delete an ADR, a postmortem, or a graveyard row. Check: `git diff --diff-filter=D --name-only origin/main -- adr postmortems` is empty and graveyard.md only gains lines.
- No performance claims, return targets, or investor language. Check: `grep -rniE 'guarantee|returns? of|alpha of|investor' VISION.md ARCHITECTURE.md` prints nothing.
- No secrets, keys, or account identifiers in examples (root rule 2).

## Gotchas
_None yet. Each entry links to the PR or postmortem where it bit us._

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/OWNER/quant-core/pull/N)) -->

## See also
- Skills: write-adr, write-plan, incident-postmortem, graveyard-entry
- adr/0000-template.md, postmortems/0000-template.md, research/graveyard.md
- ADR-0040 (agent handoff): adr/0040-agentic-decision.md
