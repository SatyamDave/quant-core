"""Tests for scripts/knowledge/scope_check.py. Stdlib only: run with
`python3 -m unittest discover -s tests/knowledge` or pytest."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/knowledge/scope_check.py"
spec = importlib.util.spec_from_file_location("scope_check", SCRIPT)
if spec is None or spec.loader is None:
    raise ImportError(SCRIPT)
scope_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope_check)

GOOD_ENTRY = "- 2026-09-27: Resync on seq gap ([#12](https://github.com/o/r/pull/12))"


class ScopeCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, path: str, lines: list[str]) -> None:
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def errors(self, *paths: str) -> list[str]:
        errors: list[str] = scope_check.check(list(paths), self.root)
        return errors

    def test_rust_file_fails(self) -> None:
        self.assertTrue(self.errors("docs/GLOSSARY.md", "engine/crates/oms/src/lib.rs"))

    def test_markdown_skills_and_rules_pass(self) -> None:
        self.write("engine/CLAUDE.md", ["# engine", "## Learned", GOOD_ENTRY, "## See also"])
        paths = [
            "engine/CLAUDE.md",
            "docs/GLOSSARY.md",
            ".claude/skills/pr-ready/SKILL.md",
            ".claude/rules/tests.md",
        ]
        self.assertEqual(self.errors(*paths), [])

    def test_non_markdown_outside_skills_fails(self) -> None:
        self.assertTrue(self.errors(".claude/settings.json"))
        self.assertTrue(self.errors(".github/workflows/ci.yml"))

    def test_over_budget_nested_claude_md_fails(self) -> None:
        self.write("engine/CLAUDE.md", ["line"] * 81)
        self.assertTrue(self.errors("engine/CLAUDE.md"))
        self.write("engine/CLAUDE.md", ["line"] * 80)
        self.assertEqual(self.errors("engine/CLAUDE.md"), [])

    def test_root_claude_md_fails(self) -> None:
        # The root CLAUDE.md holds the non-negotiable rules: human-edited only.
        self.write("CLAUDE.md", ["line"] * 10)
        self.assertTrue(self.errors("CLAUDE.md"))

    def test_agent_definitions_fail(self) -> None:
        self.assertTrue(self.errors(".claude/agents/risk-auditor.md"))
        self.assertTrue(self.errors(".claude/agents/new-agent.md"))

    def test_markdown_in_protected_zones_fails(self) -> None:
        for path in (
            "engine/crates/risk/CLAUDE.md",
            "engine/crates/risk/src/NOTES.md",
            "config/limits/README.md",
            "ops/deploy/README.md",
            "fund/README.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".claude/hooks/README.md",
            "autonomy/README.md",
            "Engine/Crates/Risk/CLAUDE.md",
        ):
            self.assertTrue(self.errors(path), path)

    def test_undated_learned_entry_fails(self) -> None:
        self.write(
            "ml/CLAUDE.md",
            ["## Learned", "- Resync on seq gap ([#12](https://github.com/o/r/pull/12))"],
        )
        self.assertTrue(self.errors("ml/CLAUDE.md"))

    def test_learned_entry_without_source_link_fails(self) -> None:
        self.write("ml/CLAUDE.md", ["## Learned", "- 2026-09-27: Resync on seq gap"])
        self.assertTrue(self.errors("ml/CLAUDE.md"))

    def test_learned_entries_must_be_newest_first(self) -> None:
        older = GOOD_ENTRY.replace("2026-09-27", "2026-09-01")
        self.write("ml/CLAUDE.md", ["## Learned", older, GOOD_ENTRY])
        self.assertTrue(self.errors("ml/CLAUDE.md"))

    def test_comment_placeholder_in_learned_passes(self) -> None:
        self.write("ml/CLAUDE.md", ["## Learned", "<!-- added only by knowledge-sync PRs -->"])
        self.assertEqual(self.errors("ml/CLAUDE.md"), [])


if __name__ == "__main__":
    unittest.main()
