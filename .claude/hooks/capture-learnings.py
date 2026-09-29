#!/usr/bin/env python3
"""Stop hook: turn signals in the session transcript into a learnings proposal.

Signals: human corrections, the same tool failing repeatedly, and gotchas Claude stated.
Writes .claude/learnings/inbox/YYYY-MM-DD-<session>.md (one file per session, rewritten on
each Stop so it stays cumulative). Never edits CLAUDE.md; triage happens in knowledge PRs.
Always exits 0: exit 2 on Stop would force Claude to keep going.
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

CORRECTION = re.compile(r"^\s*no\b[\s,.!]|\bdon'?t\b|\bdo not\b|\bwrong\b|\binstead\b", re.I)
GOTCHA = re.compile(r"\bgotcha\b|\bturns out\b|\broot cause\b|\bthe (?:issue|problem) was\b", re.I)
# User-role entries that are not a human typing: tool results, injected context, agent messages.
NOT_HUMAN = re.compile(
    r"^\s*(?:<[a-z_-]+[ >]|\[Request interrupted|Another Claude session|Caveat:)", re.I
)

REDACTIONS = [
    (re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.S), "[REDACTED PEM BLOCK]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED AWS KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED TOKEN]"),
    (
        re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
        "[REDACTED TOKEN]",
    ),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED TOKEN]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[REDACTED JWT]"),
    (re.compile(r"(?i)\b(Bearer|Basic|Token)\s+[\w.~+/=-]+"), r"\1 [REDACTED]"),
    # Credentials in a URL: scheme://user:pass@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]*:)[^@\s/]+@", re.I), r"\1[REDACTED]@"),
    # A quoted value may contain spaces; an unquoted one runs to the next space. The key may be
    # quoted too, as in JSON or a Python dict.
    (
        re.compile(
            r"""(?i)\b([\w-]*(?:api[_-]?key|secret|token|password|passwd)[\w-]*)(["']?\s*[:=]\s*)"""
            r"""("[^"]*"|'[^']*'|\S+)"""
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED HEX]"),
    # Long base64 runs with mixed case and a digit. Hyphens break the run so ordinary paths survive.
    (
        re.compile(
            r"(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])[A-Za-z0-9+/]{40,}={0,2}"
        ),
        "[REDACTED BASE64]",
    ),
]

SNIPPET = 300


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def snippet(text: str) -> str:
    # Redact before truncating so a cut cannot leave half a PEM block unmatched.
    text = " ".join(redact(text).split())
    return text[:SNIPPET] + ("..." if len(text) > SNIPPET else "")


def text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def analyse(transcript: Path) -> dict[str, list]:
    corrections: list[str] = []
    gotchas: list[str] = []
    failures: dict[str, list[str]] = defaultdict(list)
    tool_names: dict[str, str] = {}
    touched: Counter[str] = Counter()

    for line in transcript.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_names[block.get("id", "")] = block.get("name", "?")
                    path = (block.get("input") or {}).get("file_path")
                    if path:
                        touched[path] += 1
                elif block.get("type") == "text" and GOTCHA.search(block.get("text", "")):
                    gotchas.append(snippet(block["text"]))
        elif entry.get("type") == "user":
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and block.get("is_error")
                    ):
                        name = tool_names.get(block.get("tool_use_id", ""), "?")
                        failures[name].append(
                            snippet(text_of(block.get("content")) or str(block.get("content")))
                        )
            if entry.get("isMeta"):
                continue
            text = text_of(content)
            if text and not NOT_HUMAN.match(text) and CORRECTION.search(text):
                corrections.append(snippet(text))

    repeated = [
        f"{name} failed {len(errs)} times; last error: {errs[-1]}"
        for name, errs in failures.items()
        if len(errs) >= 2
    ]
    return {
        "corrections": corrections,
        "repeated": repeated,
        "gotchas": gotchas,
        "touched": list(touched),
    }


def target_file(touched: list[str], root: Path) -> str:
    """Nearest directory owning most of the files touched this session, as a CLAUDE.md path."""
    dirs: Counter[str] = Counter()
    for path in touched:
        try:
            rel = Path(path).resolve().relative_to(root.resolve())
        except ValueError:
            continue
        dirs[rel.parts[0] if len(rel.parts) > 1 else "."] += 1
    top = dirs.most_common(1)[0][0] if dirs else "."
    return "CLAUDE.md" if top == "." else f"{top}/CLAUDE.md"


def render(found: dict[str, list], target: str, session: str) -> str:
    lines = [
        f"# Learnings proposal, session {session}",
        "",
        "Written by the Stop hook from heuristics.",
        "Triage decides whether any of this becomes a rule.",
        "",
        "## What happened",
        f"- {len(found['corrections'])} human correction(s), "
        f"{len(found['repeated'])} repeatedly failing tool(s), "
        f"{len(found['gotchas'])} stated gotcha(s).",
        "",
        "## Proposed rule",
        "- Draft from the first signal below; rewrite it as a checkable MUST or NEVER first.",
        f"  > {(found['corrections'] or found['repeated'] or found['gotchas'])[0]}",
        "",
        "## Target file",
        f"- {target} (Learned or Gotchas section), or a skill or rule if it is a procedure.",
        "",
        "## Evidence",
    ]
    for title, key in (
        ("Corrections", "corrections"),
        ("Repeated failures", "repeated"),
        ("Gotchas", "gotchas"),
    ):
        if found[key]:
            lines.append(f"### {title}")
            lines.extend(f"- {item}" for item in found[key])
    return "\n".join(lines) + "\n"


def main() -> None:
    data = json.loads(sys.stdin.read() or "{}")
    transcript = Path(data.get("transcript_path") or "").expanduser()
    if not transcript.is_file():
        return
    found = analyse(transcript)
    if not (found["corrections"] or found["repeated"] or found["gotchas"]):
        return
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or Path.cwd())
    session = re.sub(r"[^A-Za-z0-9]", "", str(data.get("session_id", "")))[:8] or "unknown"
    inbox = root / ".claude" / "learnings" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    out = inbox / f"{dt.date.today().isoformat()}-{session}.md"
    out.write_text(render(found, target_file(found["touched"], root), session))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # a learnings hook must never break the session
        print(f"capture-learnings: skipped ({exc.__class__.__name__}: {exc})", file=sys.stderr)
    sys.exit(0)
