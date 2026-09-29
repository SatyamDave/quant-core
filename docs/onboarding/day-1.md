# Day 1

Goal: get the repo running and understand the shape of the system, without touching anything
protected.

## 1. Get the environment working

Work from a clone of `main` (README §5 lists prerequisites).

| Command | Purpose | Expected output |
|---|---|---|
| `just setup` | download crates and PyPI packages, create `research/.venv` | exit 0 |
| `just check` | fmt, lint, all test suites, audits | exit 0 |
| `just replay` | replay the synthetic day twice | two identical `order-log sha256` lines and `replay deterministic and matches the committed order log` |

Any other failure on a fresh clone is a bug: open an issue with the command's output rather than
working around it. The devcontainer (`.devcontainer/`) has never been built, so treat it as
unverified.

## 2. Read, in this order

1. `docs/VISION.md` — the mission and the constraints that shape every other decision here
   (no live agent in the order path, no secrets in git, gated strategy lifecycle, kill switch).
2. Root `CLAUDE.md` — the twelve non-negotiable rules, verbatim, plus the repo map. Then
   `docs/ARCHITECTURE.md`'s "planned, not built" table for what is actually true today.
3. `docs/ARCHITECTURE.md` — how `engine/`, `research/`, `ml/`, and `agents/` fit together.
4. `docs/GLOSSARY.md` — skim it once now; you'll come back to it.

## 3. Find your bearings in the code

- Read `engine/crates/core/` — the fixed-point `Price`/`Qty` types and events everything else is
  built from. Notice there's no `f64` anywhere near money.
- Skim `engine/crates/risk/` (protected — read-only for now) to see the order of the risk
  checks and their boundary tests (`cargo test -p qc-risk`).
- Read `engine/crates/replay/src/lib.rs` for the loop that ties book, strategy, risk, OMS and
  SimVenue together.
- Look at `strategies/_template/` — this is what a strategy looks like at every lifecycle gate.

## 4. Understand what you can and can't touch

Read the "Protected zones" section of `CONTRIBUTING.md`. On day 1, plan to work outside them.
Everything else is fair game for a small, well-tested PR.

## 5. Make a trivial PR

Fix a typo, improve a doc, add a test for something uncovered. Go through the real flow: branch,
commit with a conventional message, fill in the PR template, open the PR. The goal isn't the
change — it's confirming CI, the PR template, and review actually work for you before you touch
anything that matters.

Next: `docs/onboarding/week-1.md`.
