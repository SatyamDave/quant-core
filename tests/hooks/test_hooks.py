"""Feed real hook JSON on stdin to the .claude/hooks scripts and check exit codes and messages.

Run with `just test-hooks`.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / ".claude" / "hooks"
LIMITS = (REPO / "config" / "limits" / "default.toml").read_text()


def run_hook(script: str, payload: dict, env: dict | None = None, cwd: Path | None = None):
    cmd = (
        ["python3", str(HOOKS / script)]
        if script.endswith(".py")
        else ["bash", str(HOOKS / script)]
    )
    full_env = {k: v for k, v in os.environ.items() if k != "QC_ALLOW_PROTECTED"}
    full_env.update(env or {})
    return subprocess.run(  # noqa: S603 - fixed local scripts
        cmd,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd,
        check=False,
    )


def tool_call(tool: str, **tool_input) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    (tmp_path / "config" / "limits").mkdir(parents=True)
    (tmp_path / "config" / "limits" / "default.toml").write_text(LIMITS)
    return tmp_path


# guard-secrets


@pytest.mark.parametrize(
    "path",
    ["/work/.env", "/work/config/.env.local", "/work/ops/secrets/venue.json", "/work/tls.pem"],
)
def test_secret_path_read_blocked(path: str) -> None:
    result = run_hook("guard-secrets.sh", tool_call("Read", file_path=path))
    assert result.returncode == 2
    assert "guard-secrets" in result.stderr


def test_ssh_dir_read_blocked() -> None:
    result = run_hook(
        "guard-secrets.sh", tool_call("Read", file_path=str(Path.home() / ".ssh" / "id_ed25519"))
    )
    assert result.returncode == 2


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "grep KEY config/.env.prod",
        "printenv",
        "echo $VENUE_API_KEY",
        "tool --api-key abc",
    ],
)
def test_secret_printing_commands_blocked(command: str) -> None:
    result = run_hook("guard-secrets.sh", tool_call("Bash", command=command))
    assert result.returncode == 2
    assert "Rule 2" in result.stderr


@pytest.mark.parametrize(
    "command", ["just test", "ls .venv", "cargo test --workspace", "env RUST_LOG=debug cargo run"]
)
def test_ordinary_commands_allowed(command: str) -> None:
    assert run_hook("guard-secrets.sh", tool_call("Bash", command=command)).returncode == 0


def test_ordinary_read_allowed() -> None:
    assert (
        run_hook("guard-secrets.sh", tool_call("Read", file_path=str(REPO / "justfile"))).returncode
        == 0
    )


# guard-protected


def edit_limits(repo: Path, old: str, new: str) -> dict:
    return tool_call(
        "Edit",
        file_path=str(repo / "config/limits/default.toml"),
        old_string=old,
        new_string=new,
        replace_all=False,
    )


def test_protected_edit_blocked_without_flag(repo: Path) -> None:
    result = run_hook(
        "guard-protected.sh", edit_limits(repo, "stale_data_ms = 1000", "stale_data_ms = 500")
    )
    assert result.returncode == 2
    assert "protected zone config/limits/**" in result.stderr


def test_protected_edit_allowed_with_flag(repo: Path) -> None:
    risk = repo / "engine/crates/risk/src/lib.rs"
    payload = tool_call("Write", file_path=str(risk), content="// ok\n")
    assert run_hook("guard-protected.sh", payload).returncode == 2
    assert run_hook("guard-protected.sh", payload, env={"QC_ALLOW_PROTECTED": "1"}).returncode == 0


def test_tightening_limit_allowed_with_flag(repo: Path) -> None:
    payload = edit_limits(repo, 'max_notional = "25"', 'max_notional = "12.5"')
    assert run_hook("guard-protected.sh", payload, env={"QC_ALLOW_PROTECTED": "1"}).returncode == 0


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('max_position = "0.01"', 'max_position = "0.02"'),
        ("price_band_bps = 50", "price_band_bps = 51"),
        ("stale_data_ms = 1000", "stale_data_ms = 5000"),
        ("max_order_rate_per_sec = 5", ""),  # removing a limit loosens it
    ],
)
def test_loosening_limit_blocked_even_with_flag(repo: Path, old: str, new: str) -> None:
    result = run_hook(
        "guard-protected.sh", edit_limits(repo, old, new), env={"QC_ALLOW_PROTECTED": "1"}
    )
    assert result.returncode == 2
    assert "loosens risk limits" in result.stderr


def test_loosening_via_write_blocked_with_flag(repo: Path) -> None:
    payload = tool_call(
        "Write",
        file_path=str(repo / "config/limits/default.toml"),
        content=LIMITS.replace('max_daily_loss = "5"', 'max_daily_loss = "1000"'),
    )
    result = run_hook("guard-protected.sh", payload, env={"QC_ALLOW_PROTECTED": "1"})
    assert result.returncode == 2
    assert "max_daily_loss raised 5 -> 1000" in result.stderr


@pytest.mark.parametrize(
    "rel",
    [
        "autonomy/policy.toml",
        "tests/autonomy/test_x.py",
        "tests/hooks/test_x.py",
        ".github/CODEOWNERS",
    ],
)
def test_added_zones_blocked(repo: Path, rel: str) -> None:
    result = run_hook(
        "guard-protected.sh", tool_call("Write", file_path=str(repo / rel), content="x")
    )
    assert result.returncode == 2
    assert "protected zone" in result.stderr


def test_unprotected_edit_allowed(repo: Path) -> None:
    payload = tool_call("Write", file_path=str(repo / "research/features/x.py"), content="x = 1\n")
    assert run_hook("guard-protected.sh", payload).returncode == 0


# capture-learnings


def test_capture_learnings_writes_redacted_proposal(tmp_path: Path) -> None:
    fixture = Path(__file__).with_name("fixtures") / "transcript_with_correction.jsonl"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(fixture.read_text().replace("{ROOT}", str(tmp_path)))
    payload = {
        "hook_event_name": "Stop",
        "session_id": "abc12345-zz",
        "transcript_path": str(transcript),
    }

    result = run_hook("capture-learnings.py", payload, env={"CLAUDE_PROJECT_DIR": str(tmp_path)})

    assert result.returncode == 0
    (proposal,) = (tmp_path / ".claude/learnings/inbox").glob("*-abc12345.md")
    text = proposal.read_text()
    assert "don't use f64 for prices" in text
    assert "Bash failed 2 times" in text
    for section in ("## What happened", "## Proposed rule", "## Target file", "## Evidence"):
        assert section in text
    assert "engine/CLAUDE.md" in text
    for secret in (
        "AKIAIOSFODNN7EXAMPLE",
        "sk-ant-fake0123456789abcdef",
        "ghp_",
        "BEGIN RSA PRIVATE KEY",
    ):
        assert secret not in text
    assert "[REDACTED" in text


def test_capture_learnings_quiet_session_writes_nothing(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "add a test"}}) + "\n"
    )
    payload = {"session_id": "quiet", "transcript_path": str(transcript)}
    assert (
        run_hook(
            "capture-learnings.py", payload, env={"CLAUDE_PROJECT_DIR": str(tmp_path)}
        ).returncode
        == 0
    )
    assert not (tmp_path / ".claude/learnings/inbox").exists()


# guard-protected: security review fixes (PR #11)


@pytest.mark.parametrize(
    "rel",
    [
        "Config/Limits/default.toml",
        "engine/crates/RISK/src/lib.rs",
        "config/./limits/../limits/default.toml",
        "research/../config/limits/default.toml",
    ],
)
def test_protected_match_ignores_case_and_dot_segments(repo: Path, rel: str) -> None:
    result = run_hook(
        "guard-protected.sh", tool_call("Write", file_path=str(repo) + "/" + rel, content="x")
    )
    assert result.returncode == 2
    assert "protected zone" in result.stderr


@pytest.mark.parametrize(
    "rel",
    [
        ".claude/settings.local.json",
        ".claude/agents/risk-auditor.md",
        "scripts/ci/ai_gate.py",
        "tests/ci/test_ai_gate.py",
        "scripts/knowledge/scope_check.py",
    ],
)
def test_review_added_zones_blocked(repo: Path, rel: str) -> None:
    result = run_hook(
        "guard-protected.sh", tool_call("Write", file_path=str(repo / rel), content="{}")
    )
    assert result.returncode == 2
    assert "protected zone" in result.stderr


def test_settings_denies_edits_to_review_added_zones() -> None:
    deny = json.loads((REPO / ".claude" / "settings.json").read_text())["permissions"]["deny"]
    for rule in (
        "Edit(/.claude/settings.local.json)",
        "Edit(/.claude/agents/risk-auditor.md)",
        "Edit(/scripts/ci/ai_gate.py)",
        "Edit(/tests/ci/**)",
        "Edit(/scripts/knowledge/scope_check.py)",
    ):
        assert rule in deny


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('max_position = "0.01"', 'max_position = "NaN"'),
        ('max_position = "0.01"', 'max_position = "-Infinity"'),
        ("max_order_rate_per_sec = 5", "max_order_rate_per_sec = nan"),
        ("stale_data_ms = 1000", "stale_data_ms = inf"),
        ('max_notional = "500"', 'max_notional = "lots"'),
        ('max_notional = "500"', "max_notional = true"),
    ],
)
def test_non_finite_or_non_numeric_limit_blocked_with_flag(repo: Path, old: str, new: str) -> None:
    result = run_hook(
        "guard-protected.sh", edit_limits(repo, old, new), env={"QC_ALLOW_PROTECTED": "1"}
    )
    assert result.returncode == 2
    assert "guard-protected" in result.stderr


def test_hook_exception_blocks(repo: Path) -> None:
    payload = tool_call("Write", file_path=str(repo / "config/limits/default.toml"), content=5)
    result = run_hook("guard-protected.sh", payload, env={"QC_ALLOW_PROTECTED": "1"})
    assert result.returncode == 2
    assert "guard-protected" in result.stderr


def write_new_limits(repo: Path, name: str, content: str):
    payload = tool_call("Write", file_path=str(repo / "config/limits" / name), content=content)
    return run_hook("guard-protected.sh", payload, env={"QC_ALLOW_PROTECTED": "1"})


@pytest.mark.parametrize(
    "content",
    [
        'max_position = "1000000"\n',
        "stale_data_ms = 60000\n",
        'max_leverage = "3"\n',  # unknown key
        '[venue]\nmax_position = "0.001"\n',  # unknown table key
        "max_position = [1]\n",
        "not toml = =\n",
    ],
)
def test_new_limits_file_looser_than_default_blocked(repo: Path, content: str) -> None:
    result = write_new_limits(repo, "strat-x.toml", content)
    assert result.returncode == 2
    assert "guard-protected" in result.stderr


def test_new_limits_file_tighter_than_default_allowed(repo: Path) -> None:
    content = 'max_position = "0.005"\nmax_notional = "20"\nstale_data_ms = 500\n'
    assert write_new_limits(repo, "strat-x.toml", content).returncode == 0


def test_non_toml_under_limits_blocked_with_flag(repo: Path) -> None:
    result = write_new_limits(repo, "strat-x.json", '{"max_position": "1000000"}')
    assert result.returncode == 2


# capture-learnings redaction


def load_capture_learnings():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "capture_learnings", HOOKS / "capture-learnings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"api_key": "AbC123xyzAbC123xyz"}', "AbC123xyzAbC123xyz"),
        ("{'password': 'hunter2 with spaces'}", "hunter2"),
        ('"client_secret" : "s3cr3tvalue"', "s3cr3tvalue"),
        ("Authorization: Bearer abc.def-ghi_jkl", "abc.def-ghi_jkl"),
        (
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl here",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
        ("postgres://admin:S3cr3tP4ss@db.internal:5432/x", "S3cr3tP4ss"),
        ("https://user:pa%40ss@example.com/", "pa%40ss"),
    ],
)
def test_snippet_redacts_common_secret_shapes(text: str, secret: str) -> None:
    out = load_capture_learnings().snippet(text)
    assert secret not in out
    assert "[REDACTED" in out
