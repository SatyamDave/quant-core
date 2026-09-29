import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "gen_status", Path(__file__).resolve().parents[2] / "scripts" / "docs" / "gen_status.py"
)
gen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen)


def test_untrusted_pr_titles_are_escaped_in_markdown_and_html():
    hostile = [
        {
            "number": 7,
            "title": "x | <script>alert(1)</script>\n## boom",
            "state": "OPEN",
            "author": {"login": "a"},
        }
    ]
    md, block = gen.build(hostile, [])
    row = next(line for line in md.splitlines() if line.startswith("| [#7]"))
    assert "<script>" not in row
    assert "\\|" in row
    assert "\n## boom" not in md
    assert "<script>" not in block


def test_output_is_deterministic_for_the_same_inputs():
    prs = [{"number": 1, "title": "t", "state": "MERGED", "mergedAt": "2026-09-28T00:00:00Z"}]
    assert gen.build(prs, []) == gen.build(prs, [])


def test_inject_replaces_only_between_markers_and_requires_them():
    page = f"a{gen.START}old{gen.END}b"
    assert gen.inject(page, f"{gen.START}new{gen.END}") == f"a{gen.START}new{gen.END}b"
    with pytest.raises(SystemExit):
        gen.inject("no markers", "x")
