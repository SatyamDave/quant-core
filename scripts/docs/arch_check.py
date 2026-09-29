#!/usr/bin/env python3
"""Flag a PR as a major architecture change and check it documents itself. Standard library only.

  arch_check.py --files FILE --body FILE [--diff FILE] [--actor NAME] [--claude-md PATH]

FILE for --files is one changed repo path per line (from `git diff --name-only base...head`).
FILE for --body is the PR body, read as plain text (never interpolated into a shell). FILE for
--diff, if given, is the unified diff (`git diff base...head`); without it, the two heuristics
that need line-level content (engine crate public items, root CLAUDE.md rule text) are skipped.

A change is "major architecture" (ADR-0040, docs/adr/0040-agentic-decision.md) if it touches:
  - a public item (`pub trait|pub struct|pub enum|pub fn`) in an `engine/crates/*/src/lib.rs`;
  - schemas/**, agent/src/**/decider*, docs/adr/**, config/limits/**, .github/workflows/**,
    autonomy/POLICY.yaml;
  - root CLAUDE.md rule text (a changed numbered-rule line);
  - a new top-level directory (anything not already known from root CLAUDE.md's Repo map, plus
    scripts/, tests/, .github/, .claude/, .devcontainer/).

For a major change the PR body must have a non-empty "Architecture impact:" section (not just
"none": that value is only valid for a non-major change), the diff must touch docs/ARCHITECTURE.md
or a file under docs/adr/, and a flagged engine crate's own CLAUDE.md must be touched too.

Every PR body (major or not) must have a non-empty "Moves us toward the first agentic trade by:"
section (the north-star rule). Bot actors (name ending "[bot]") are exempt from everything.

Exit 0 when nothing is missing (or the actor is exempt), 1 otherwise. Always prints a markdown
summary; a missing README.md architecture update is a warning in that summary, not a failure.
"""

import argparse
import re
import sys
from pathlib import Path

INFRA_TOP_LEVEL = {"scripts", "tests", ".github", ".claude", ".devcontainer"}
REPO_MAP_ENTRY = re.compile(r"^([A-Za-z0-9_.-]+)/\s")
NUMBERED_RULE_LINE = re.compile(r"^\d+\.\s")
PUBLIC_ITEM = re.compile(r"\b(pub trait|pub struct|pub enum|pub fn)\b")
ENGINE_LIB_RS = re.compile(r"^engine/crates/([^/]+)/src/lib\.rs$")
NORTH_STAR = "Moves us toward the first agentic trade by:"
IMPACT = "Architecture impact:"


def is_bot(actor: str) -> bool:
    return actor.endswith("[bot]")


def known_top_level_dirs(claude_md: Path) -> set[str]:
    """Directory names already declared in root CLAUDE.md's Repo map, plus fixed infra dirs."""
    dirs = set(INFRA_TOP_LEVEL)
    try:
        text = claude_md.read_text(encoding="utf-8")
    except OSError:
        return dirs
    in_map = False
    for line in text.splitlines():
        if line.strip() == "## Repo map":
            in_map = True
            continue
        if not in_map:
            continue
        if line.startswith("## "):
            break
        match = REPO_MAP_ENTRY.match(line)
        if match:
            dirs.add(match.group(1))
    return dirs


def changed_lines_by_file(diff_text: str) -> dict[str, list[str]]:
    """Map each touched file to its added/removed content lines, from a unified diff."""
    result: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current = None
            continue
        if line.startswith("+++ "):
            path = line[4:].strip()
            path = path.removeprefix("b/")
            current = None if path == "/dev/null" else path
            if current is not None:
                result.setdefault(current, [])
            continue
        if current is None or line.startswith(("+++", "---")):
            continue
        if line[:1] in ("+", "-"):
            result.setdefault(current, []).append(line[1:])
    return result


def section_text(body: str, label: str) -> str | None:
    """Text of a "<label> <inline>" line, or the body of a "<label>" heading. None if absent."""
    # Accept "Label: text", "## Label:" and "## Label" (headings are often written without the
    # colon), case-insensitively.
    core = label.rstrip(":").strip().lower()
    lines = body.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#").strip()
        if not stripped.lower().startswith(core):
            continue
        inline = stripped[len(core) :].lstrip(":").strip()
        if inline:
            return inline
        collected = []
        for later in lines[i + 1 :]:
            if later.strip().startswith("#"):
                break
            if later.strip():
                collected.append(later.strip())
        return " ".join(collected)
    return None


def detect_major_changes(files: list[str], diff_text: str, claude_md: Path) -> list[str]:
    reasons = []
    by_file = changed_lines_by_file(diff_text)
    for path in files:
        if path.startswith("schemas/"):
            reasons.append(f"schemas/** changed: {path}")
        elif path.startswith("agent/src/") and "decider" in Path(path).name:
            reasons.append(f"agent decider changed: {path}")
        elif path.startswith("docs/adr/"):
            reasons.append(f"ADR changed: {path}")
        elif path.startswith("config/limits/"):
            reasons.append(f"config/limits/** changed: {path}")
        elif path.startswith(".github/workflows/"):
            reasons.append(f".github/workflows/** changed: {path}")
        elif path == "autonomy/POLICY.yaml":
            reasons.append("autonomy/POLICY.yaml changed")
    if path_has_numbered_rule_edit(by_file.get("CLAUDE.md", [])):
        reasons.append("root CLAUDE.md rule text changed")
    for path, lines in by_file.items():
        match = ENGINE_LIB_RS.match(path)
        if match and any(PUBLIC_ITEM.search(line) for line in lines):
            reasons.append(f"public interface changed: {path}")
    known = known_top_level_dirs(claude_md)
    new_dirs = {path.split("/", 1)[0] for path in files if "/" in path} - known
    reasons.extend(f"new top-level directory: {d}/" for d in sorted(new_dirs))
    return reasons


def path_has_numbered_rule_edit(lines: list[str]) -> bool:
    return any(NUMBERED_RULE_LINE.match(line) for line in lines)


def flagged_engine_crates(files: list[str], diff_text: str) -> set[str]:
    by_file = changed_lines_by_file(diff_text)
    crates = set()
    for path, lines in by_file.items():
        match = ENGINE_LIB_RS.match(path)
        if match and any(PUBLIC_ITEM.search(line) for line in lines):
            crates.add(match.group(1))
    return crates


def cited_existing_adrs(impact: str, adr_dir: Path) -> list[str]:
    """ADR numbers cited as ADR-NNNN in the impact section that exist in docs/adr/.

    Implementing an already-recorded decision needs no new ADR; citing one that does not
    exist does not count.
    """
    return [
        n
        for n in sorted(set(re.findall(r"\bADR-(\d{4})\b", impact)))
        if any(adr_dir.glob(f"{n}-*.md"))
    ]


def check(
    files: list[str], body: str, diff_text: str, actor: str, claude_md: Path
) -> tuple[bool, str]:
    if is_bot(actor):
        return True, f"arch_check: actor {actor!r} is a bot, exempt.\n"

    missing: list[str] = []
    warnings: list[str] = []

    north_star = section_text(body, NORTH_STAR)
    if not north_star:
        missing.append(f'PR body needs a non-empty "{NORTH_STAR}" section.')

    major = detect_major_changes(files, diff_text, claude_md)
    if major:
        impact = section_text(body, IMPACT)
        if not impact or impact.strip().lower() == "none":
            missing.append(
                f'Major change: PR body needs a non-empty "{IMPACT}" section '
                '("none" is only valid for a non-major change).'
            )
        touches_adr = any(f.startswith("docs/adr/") for f in files)
        cited = cited_existing_adrs(impact or "", claude_md.parent / "docs" / "adr")
        if "docs/ARCHITECTURE.md" not in files and not touches_adr and not cited:
            missing.append(
                "Major change: PR must touch docs/ARCHITECTURE.md or a docs/adr/ file, "
                "or cite an existing ADR (e.g. ADR-0040) in its Architecture impact section."
            )
        for crate in sorted(flagged_engine_crates(files, diff_text)):
            crate_doc = f"engine/crates/{crate}/CLAUDE.md"
            if crate_doc not in files:
                missing.append(f"Major change: public interface changed but {crate_doc} was not.")
        if "README.md" not in files:
            warnings.append(
                "README.md was not touched; check whether its architecture section needs it."
            )

    lines = ["# Architecture check", "", "## Major changes detected"]
    if major:
        lines.extend(f"- {r}" for r in major)
    else:
        lines.append("- None detected.")
    if missing:
        lines += ["", "## Missing requirements"]
        lines.extend(f"- {m}" for m in missing)
    if warnings:
        lines += ["", "## Warnings"]
        lines.extend(f"- {w}" for w in warnings)
    lines += ["", "## Result", "FAIL" if missing else "PASS"]
    return not missing, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", required=True, type=Path)
    parser.add_argument("--body", required=True, type=Path)
    parser.add_argument("--diff", type=Path, default=None)
    parser.add_argument("--actor", default="")
    parser.add_argument("--claude-md", type=Path, default=Path("CLAUDE.md"))
    args = parser.parse_args(argv)

    raw_files = args.files.read_text(encoding="utf-8").splitlines()
    files = [line.strip() for line in raw_files if line.strip()]
    body = args.body.read_text(encoding="utf-8")
    diff_text = args.diff.read_text(encoding="utf-8") if args.diff else ""

    ok, summary = check(files, body, diff_text, args.actor, args.claude_md)
    print(summary)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
