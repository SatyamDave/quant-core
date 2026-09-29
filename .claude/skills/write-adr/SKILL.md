---
name: write-adr
description: Write an architecture decision record. Use when choosing a library, engine, venue, storage format, or any decision that is expensive to reverse.
---
## Steps
1. Find the next number in docs/adr/ (four digits, e.g. 0006).
2. Copy docs/adr/0000-template.md to docs/adr/NNNN-<slug>.md. Sections: Status, Context, Decision, Alternatives, Consequences, Review date.
3. Verify every version, license, and API claim against the current source (release page, README, docs). Cite links.
4. Flag copyleft licenses and anything needing legal or jurisdiction confirmation for a human.
5. If this replaces an ADR, set the old one's Status to "Superseded by NNNN". Never delete it.
6. Open a PR; decisions stay Proposed until a human accepts them.
