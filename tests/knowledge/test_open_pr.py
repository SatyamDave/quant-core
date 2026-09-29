"""Tests for scripts/knowledge/open_pr.sh against a local origin and a fake `gh`."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr list") printf '%s' "${FAKE_GH_OPEN:-}"; exit "${FAKE_GH_LIST_RC:-0}" ;;
  "pr create") exit "${FAKE_GH_CREATE_RC:-0}" ;;
esac
exit 1
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed local git/bash in a temp repo
        ["git", "-C", str(cwd), *args],  # noqa: S607 - fixed local git in a temp repo
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class OpenPrTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)  # noqa: S603, S607 - fixed local git/bash in a temp repo
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.work)], check=True)  # noqa: S603, S607 - fixed local git/bash in a temp repo
        (self.work / "scripts/knowledge").mkdir(parents=True)
        for name in ("scope_check.py", "open_pr.sh"):
            src = REPO / "scripts/knowledge" / name
            (self.work / "scripts/knowledge" / name).write_text(src.read_text())
        (self.work / "docs").mkdir()
        (self.work / "docs/notes.md").write_text("old\n")
        git(self.work, "add", ".")
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.proposal = tmp / "proposal"
        self.proposal.mkdir()
        (self.work / "docs/notes.md").write_text("new\n")
        (self.proposal / "changes.patch").write_text(git(self.work, "diff") + "\n")
        git(self.work, "checkout", "-q", "--", ".")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH)
        (bin_dir / "gh").chmod(0o755)
        self.log = tmp / "gh.log"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_GH_LOG": str(self.log),
            "GITHUB_WORKFLOW": "knowledge-sync",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "1",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_script(self, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 - fixed local git/bash in a temp repo
            [  # noqa: S607 - fixed local git/bash in a temp repo
                "bash",
                "scripts/knowledge/open_pr.sh",
                str(self.proposal),
                "knowledge/abc",
                "docs: x",
            ],
            cwd=self.work,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
        )

    def gh_calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def push_stale_branch(self) -> str:
        git(self.work, "push", "-q", "origin", "HEAD:refs/heads/knowledge/abc")
        return git(self.work, "rev-parse", "HEAD")

    def remote_branch(self) -> str:
        return git(self.origin, "rev-parse", "--verify", "-q", "refs/heads/knowledge/abc")

    def test_open_pr_for_branch_skips(self) -> None:
        stale = self.push_stale_branch()
        result = self.run_script(FAKE_GH_OPEN="42")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("pr create", self.gh_calls())
        self.assertEqual(self.remote_branch(), stale)

    def test_existing_branch_without_open_pr_still_opens_pr(self) -> None:
        stale = self.push_stale_branch()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pr list --head knowledge/abc --state open", self.gh_calls())
        self.assertIn("pr create", self.gh_calls())
        self.assertNotEqual(self.remote_branch(), stale)

    def test_failed_pr_create_exits_nonzero_and_keeps_branch(self) -> None:
        result = self.run_script(FAKE_GH_CREATE_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not open the PR", result.stderr)
        self.assertTrue(self.remote_branch())

    def test_failed_pr_lookup_exits_nonzero(self) -> None:
        result = self.run_script(FAKE_GH_LIST_RC="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("pr create", self.gh_calls())


if __name__ == "__main__":
    unittest.main()
