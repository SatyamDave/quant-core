#!/usr/bin/env python3
"""Scope check for knowledge PRs (spec section 8.2).

Reads changed repo paths (arguments, or one per line on stdin) and fails if:
- a path is outside **/*.md, .claude/skills/**, .claude/rules/**;
- a path is the root CLAUDE.md, under .claude/agents/, or inside a protected zone,
  markdown included (compared case-insensitively);
- a changed nested CLAUDE.md exceeds its line budget of 80;
- an entry under "## Learned" in a changed CLAUDE.md is not
  `- YYYY-MM-DD: <lesson> ([<label>](https://github.com/...))`,
  the entries are not newest first, or there are more than 15.

Run from the repository root. Deleted paths are still scope-checked.
"""

from __future__ import annotations

import datetime
import re
import sys
from pathlib import Path

ALLOWED_PREFIXES = (".claude/skills/", ".claude/rules/")
# Human-edited only: the root rules, agent definitions and every protected zone.
FORBIDDEN_PREFIXES = (
    ".claude/agents/",
    ".claude/hooks/",
    ".github/",
    "autonomy/",
    "config/limits/",
    "engine/crates/risk/",
    "fund/",
    "ops/deploy/",
)
NESTED_BUDGET = 80
MAX_LEARNED = 15
LEARNED_ENTRY = re.compile(
    r"^- (?P<date>\d{4}-\d{2}-\d{2}): \S.* \(\[[^\]]+\]\(https://github\.com/\S+\)\)$"
)
COMMENT = re.compile(r"^<!--.*-->$")


def in_scope(path: str) -> bool:
    return path.endswith(".md") or path.startswith(ALLOWED_PREFIXES)


def forbidden(path: str) -> bool:
    folded = path.casefold()
    return folded == "claude.md" or folded.startswith(FORBIDDEN_PREFIXES)


def check_learned(path: str, lines: list[str]) -> list[str]:
    errors: list[str] = []
    in_section = False
    dates: list[datetime.date] = []
    for number, line in enumerate(lines, start=1):
        if line.startswith("## "):
            in_section = line.strip() == "## Learned"
            continue
        stripped = line.strip()
        if not in_section or not stripped or COMMENT.match(stripped):
            continue
        match = LEARNED_ENTRY.match(stripped)
        date = None
        if match:
            try:
                date = datetime.date.fromisoformat(match["date"])
            except ValueError:
                pass
        if date is None:
            errors.append(
                f"{path}:{number}: Learned entry must be "
                "'- YYYY-MM-DD: <lesson> ([#N](https://github.com/...))'"
            )
            continue
        if dates and date > dates[-1]:
            errors.append(f"{path}:{number}: Learned entries must be newest first")
        dates.append(date)
    if len(dates) > MAX_LEARNED:
        errors.append(f"{path}: {len(dates)} Learned entries, max {MAX_LEARNED}")
    return errors


def check(paths: list[str], root: Path) -> list[str]:
    errors: list[str] = []
    for path in paths:
        if forbidden(path):
            errors.append(
                f"{path}: knowledge PRs may not touch the root CLAUDE.md, .claude/agents/ "
                "or a protected zone"
            )
            continue
        if not in_scope(path):
            errors.append(
                f"{path}: knowledge PRs may only touch **/*.md, .claude/skills/**, .claude/rules/**"
            )
            continue
        file = root / path
        if Path(path).name != "CLAUDE.md" or not file.is_file():
            continue
        lines = file.read_text(encoding="utf-8").splitlines()
        if len(lines) > NESTED_BUDGET:
            errors.append(f"{path}: {len(lines)} lines, budget {NESTED_BUDGET}")
        errors.extend(check_learned(path, lines))
    return errors


def main(argv: list[str]) -> int:
    paths = argv or [line.strip() for line in sys.stdin]
    paths = [p.removeprefix("./") for p in paths if p]
    if not paths:
        print("scope_check: no changed paths given", file=sys.stderr)
        return 1
    errors = check(paths, Path.cwd())
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print(f"scope_check: {len(paths)} path(s) OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
