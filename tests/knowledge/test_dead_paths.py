"""Tests for scripts/knowledge/dead_paths.py (stdlib only)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/knowledge/dead_paths.py"
spec = importlib.util.spec_from_file_location("dead_paths", SCRIPT)
if spec is None or spec.loader is None:
    raise ImportError(SCRIPT)
dead_paths = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dead_paths)


class DeadPathsTest(unittest.TestCase):
    def test_flags_only_missing_repo_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "engine/crates/risk").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "engine/CLAUDE.md").write_text(
                "See `crates/risk/` and `crates/gone/`.\n"
                "Run `just check`, see `https://x.io/a/b`, `engine/crates/*/src`.\n",
                encoding="utf-8",
            )
            rules = root / ".claude/rules"
            rules.mkdir(parents=True)
            (rules / "tests.md").write_text(
                "Read `docs/missing.md`. Commit as `feat(engine/oms): x`.\n", encoding="utf-8"
            )
            (root / "docs/README.md").write_text("`docs/also-missing.md`\n", encoding="utf-8")

            self.assertEqual(
                dead_paths.dead_paths(root),
                [".claude/rules/tests.md:1: docs/missing.md", "engine/CLAUDE.md:1: crates/gone/"],
            )


if __name__ == "__main__":
    unittest.main()
