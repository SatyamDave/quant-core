"""scripts/docs/arch_check.py flags major architecture changes and their required PR body text."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/docs/arch_check.py"
spec = importlib.util.spec_from_file_location("arch_check", SCRIPT)
assert spec and spec.loader
arch_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arch_check)

REPO_MAP = """# quant-core

## Repo map
```
engine/      Rust hot path
research/    Python research
docs/        Vision, architecture, ADRs
config/      limits, venues, environments
autonomy/    autonomy policy
agent/       TypeScript agent service
schemas/     Handoff contract
evals/       Eval suites
```

## Commands
"""

NORTH_STAR = "## Moves us toward the first agentic trade by:\nKeeps CI honest about scope.\n"
IMPACT_NONE = "## Architecture impact:\nnone\n"


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.write_text(text)
    return path


def run(
    tmp_path: Path, files: list[str], body: str, diff: str = "", actor: str = ""
) -> tuple[int, str]:
    claude_md = write(tmp_path, "CLAUDE.md", REPO_MAP)
    files_file = write(tmp_path, "files.txt", "\n".join(files) + "\n")
    body_file = write(tmp_path, "body.md", body)
    args = [
        "--files",
        str(files_file),
        "--body",
        str(body_file),
        "--actor",
        actor,
        "--claude-md",
        str(claude_md),
    ]
    if diff:
        diff_file = write(tmp_path, "diff.txt", diff)
        args += ["--diff", str(diff_file)]
    code = arch_check.main(args)
    return code, arch_check.check(files, body, diff, actor, claude_md)[1]


def test_minor_change_passes(tmp_path: Path) -> None:
    code, summary = run(tmp_path, ["docs/runbooks/new.md"], NORTH_STAR + IMPACT_NONE)
    assert code == 0
    assert "None detected." in summary
    assert "PASS" in summary


def test_major_change_without_impact_section_fails(tmp_path: Path) -> None:
    code, summary = run(tmp_path, ["config/limits/default.toml"], NORTH_STAR)
    assert code == 1
    assert "config/limits/** changed" in summary
    assert "Architecture impact" in summary
    assert "FAIL" in summary


def test_major_change_with_adr_and_impact_passes(tmp_path: Path) -> None:
    body = NORTH_STAR + "## Architecture impact:\nAdds ADR-0040, the agent handoff.\n"
    code, summary = run(tmp_path, ["docs/adr/0040-agentic-decision.md", "README.md"], body)
    assert code == 0, summary
    assert "PASS" in summary


def test_missing_north_star_fails(tmp_path: Path) -> None:
    code, summary = run(tmp_path, ["docs/runbooks/new.md"], IMPACT_NONE)
    assert code == 1
    assert "Moves us toward the first agentic trade by" in summary


def test_bot_actor_is_exempt(tmp_path: Path) -> None:
    code, summary = run(tmp_path, ["config/limits/default.toml"], "", actor="dependabot[bot]")
    assert code == 0
    assert "bot" in summary


def test_none_impact_on_major_change_still_fails(tmp_path: Path) -> None:
    code, _ = run(tmp_path, ["autonomy/POLICY.yaml"], NORTH_STAR + IMPACT_NONE)
    assert code == 1


def test_new_top_level_directory_is_major(tmp_path: Path) -> None:
    body = NORTH_STAR + "## Architecture impact:\nNew ledger directory.\n"
    code, summary = run(tmp_path, ["ledger/CLAUDE.md", "docs/ARCHITECTURE.md"], body)
    assert "new top-level directory: ledger/" in summary
    assert code == 0, summary


def test_known_top_level_directory_from_repo_map_is_not_new(tmp_path: Path) -> None:
    code, summary = run(tmp_path, ["agent/CLAUDE.md"], NORTH_STAR + IMPACT_NONE)
    assert "new top-level directory" not in summary
    assert code == 0, summary


def test_engine_public_interface_change_requires_its_own_claude_md(tmp_path: Path) -> None:
    diff = (
        "diff --git a/engine/crates/risk/src/lib.rs b/engine/crates/risk/src/lib.rs\n"
        "--- a/engine/crates/risk/src/lib.rs\n"
        "+++ b/engine/crates/risk/src/lib.rs\n"
        "+pub fn new_check() {}\n"
    )
    body = NORTH_STAR + "## Architecture impact:\nNew risk check.\n"
    code, summary = run(
        tmp_path, ["engine/crates/risk/src/lib.rs", "docs/ARCHITECTURE.md"], body, diff=diff
    )
    assert code == 1
    assert "engine/crates/risk/CLAUDE.md" in summary

    code_ok, _ = run(
        tmp_path,
        ["engine/crates/risk/src/lib.rs", "engine/crates/risk/CLAUDE.md", "docs/ARCHITECTURE.md"],
        body,
        diff=diff,
    )
    assert code_ok == 0


def test_root_claude_md_rule_text_change_is_major(tmp_path: Path) -> None:
    diff = (
        "diff --git a/CLAUDE.md b/CLAUDE.md\n"
        "--- a/CLAUDE.md\n"
        "+++ b/CLAUDE.md\n"
        "-1. Old rule.\n"
        "+1. New rule.\n"
    )
    body = NORTH_STAR + "## Architecture impact:\nAmended rule 1.\n"
    code, summary = run(tmp_path, ["CLAUDE.md", "docs/ARCHITECTURE.md"], body, diff=diff)
    assert "root CLAUDE.md rule text changed" in summary
    assert code == 0, summary


def test_readme_warning_does_not_fail(tmp_path: Path) -> None:
    body = NORTH_STAR + "## Architecture impact:\nNew limit.\n"
    code, summary = run(tmp_path, ["config/limits/default.toml", "docs/ARCHITECTURE.md"], body)
    assert code == 0
    assert "README.md was not touched" in summary


@pytest.mark.parametrize("actor", ["dependabot[bot]", "renovate[bot]"])
def test_is_bot(actor: str) -> None:
    assert arch_check.is_bot(actor)


def test_is_not_bot() -> None:
    assert not arch_check.is_bot("octocat")


def test_citing_an_existing_adr_satisfies_the_doc_requirement(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0040-agentic-decision.md").write_text("# ADR-0040\n")
    body = NORTH_STAR + "Architecture impact: implements ADR-0040's handoff contract.\n"
    code, summary = run(tmp_path, ["schemas/decision/v1/decision.schema.json"], body)
    assert code == 0, summary


def test_citing_a_missing_adr_does_not_count(tmp_path: Path) -> None:
    body = NORTH_STAR + "Architecture impact: implements ADR-0999.\n"
    code, summary = run(tmp_path, ["schemas/decision/v1/decision.schema.json"], body)
    assert code == 1
    assert "cite an existing ADR" in summary


def test_heading_without_colon_counts_as_the_section(tmp_path: Path) -> None:
    body = (
        "## Moves us toward the first agentic trade by\n\nwiring the gateway.\n\n"
        "## Architecture impact\n\nimplements ADR-0040.\n"
    )
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0040-agentic-decision.md").write_text("# ADR-0040\n")
    code, summary = run(tmp_path, ["schemas/decision/v1/decision.schema.json"], body)
    assert code == 0, summary


def test_empty_heading_section_still_fails(tmp_path: Path) -> None:
    body = "## Moves us toward the first agentic trade by\n\n## Architecture impact\nnone\n"
    code, _ = run(tmp_path, ["docs/runbooks/new.md"], body)
    assert code == 1
