#!/usr/bin/env python3
"""List backticked repo paths in agent instructions that no longer exist.

Scans every CLAUDE.md and every markdown file under a .claude/rules or
.claude/skills directory. A backticked token counts as a path when it has a
"/" and its first segment exists at the repository root or next to the file
(nested CLAUDE.md files use paths relative to their own directory). Tokens with
spaces, globs, placeholders or URLs are ignored. Prints `file:line: path` and
exits 1 when anything is dead. Used by gardener.yml.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

SKIP_DIRS = {".git", "target", ".venv", "node_modules"}
TOKEN = re.compile(r"`([^`\s]+)`")
NOT_A_PATH = re.compile(r"[*?{}<>$]|://")


def instruction_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.md")):
        parts = path.relative_to(root).parts
        if SKIP_DIRS.intersection(parts):
            continue
        in_claude_dir = any(a == ".claude" and b in ("rules", "skills") for a, b in pairwise(parts))
        if path.name == "CLAUDE.md" or in_claude_dir:
            yield path


def dead_paths(root: Path) -> list[str]:
    found: list[str] = []
    for file in instruction_files(root):
        bases = (root, file.parent)
        text = file.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            for token in TOKEN.findall(line):
                if "/" not in token or NOT_A_PATH.search(token):
                    continue
                candidate = token.removeprefix("./").rstrip("/")
                first = candidate.split("/", 1)[0]
                relevant = [b for b in bases if first and (b / first).exists()]
                if relevant and not any((b / candidate).exists() for b in relevant):
                    found.append(f"{file.relative_to(root)}:{number}: {token}")
    return found


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else Path.cwd()
    found = dead_paths(root.resolve())
    for entry in found:
        print(entry)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
