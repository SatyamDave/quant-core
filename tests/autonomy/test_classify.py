import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("classify", ROOT / "autonomy" / "classify.py")
classify_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(classify_mod)

POLICY = classify_mod.load_policy(ROOT / "autonomy" / "POLICY.yaml")
HEAD = "abc123"


def classify(paths, labels=()):
    return classify_mod.classify(paths, labels, POLICY)


def review(login, state="APPROVED", commit=HEAD, assoc="COLLABORATOR", kind="User"):
    return {"user": {"login": login, "type": kind}, "state": state, "commit_id": commit, "author_association": assoc}


# Repository permission per login, as `gh api repos/{repo}/collaborators/{user}/permission` reports it.
PERMISSIONS = {"alice": "write", "bob": "admin", "carol": "write", "author": "admin"}


def gate(result, reviews, labels=()):
    approvers, blockers = classify_mod.count_approvals(reviews, HEAD, "author", PERMISSIONS)
    return classify_mod.gate(result, approvers, labels, blockers, POLICY)


def test_t0_registry_entry_auto_merges_without_approval():
    r = classify(["research/registry/experiments/2026-09-27-momentum.json"])
    assert r["tier"] == "T0"
    assert r["required_human_approvals"] == 0
    assert r["auto_merge_allowed"] is True
    assert gate(r, [])[0] is True


def test_script_in_registry_is_not_t0():
    assert classify(["research/registry/log_run.py"])["tier"] == "T2"


def test_t2_code_pr_needs_one_human():
    r = classify(["research/features/momentum.py"])
    assert r["tier"] == "T2"
    assert r["auto_merge_allowed"] is False
    assert gate(r, [])[0] is False
    assert gate(r, [review("alice")])[0] is True


def test_config_limits_blocked_even_with_one_approval():
    r = classify(["config/limits/prod.yaml"])
    assert r["tier"] == "T3"
    assert r["required_human_approvals"] == 2
    assert gate(r, [review("alice")], ["agent-approved:risk-auditor"])[0] is False
    # Two humans without the risk-auditor review is still blocked.
    assert gate(r, [review("alice"), review("bob")])[0] is False
    assert gate(r, [review("alice"), review("bob")], ["agent-approved:risk-auditor"])[0] is True


def test_mixed_pr_takes_highest_tier():
    r = classify(["docs/research/graveyard.md", "research/features/x.py", "engine/crates/risk/src/lib.rs"])
    assert r["tier"] == "T3"
    assert classify(["docs/research/graveyard.md", "research/features/x.py"])["tier"] == "T2"


def test_unknown_path_defaults_to_t2():
    r = classify(["somewhere/new/thing.txt"])
    assert r["tier"] == "T2"
    assert "default deny" in r["reasons"][0]


def test_empty_change_list_defaults_to_t2():
    assert classify([])["tier"] == "T2"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".claude/hooks/guard-protected.sh",
        ".claude/settings.json",
        "autonomy/POLICY.yaml",
        "autonomy/scorecards/code-reviewer.md",
    ],
)
def test_never_auto_merge_paths(path):
    r = classify([path])
    assert r["tier"] == "T3"
    assert r["auto_merge_allowed"] is False
    # Riding along with a T0 file does not change that.
    assert classify(["docs/GLOSSARY.md", path])["auto_merge_allowed"] is False


def test_never_auto_merge_holds_even_if_tier_were_t0():
    loose = json.loads(json.dumps(POLICY))
    loose["protected_paths"] = []
    loose["rules"].insert(0, {"pattern": "**", "tier": "T0"})
    r = classify_mod.classify([".github/workflows/ci.yml"], (), loose)
    assert r["tier"] == "T0"
    assert r["auto_merge_allowed"] is False


def test_labels_raise_but_never_lower():
    assert classify(["docs/GLOSSARY.md"], ["model-promotion"])["tier"] == "T3"
    assert classify(["config/limits/a.yaml"], ["tier:t2"])["tier"] == "T3"


def test_t1_doc_fix_needs_a_human_while_t1_auto_merge_is_off():
    r = classify(["docs/onboarding/day-1.md"])
    assert r["tier"] == "T1"
    assert r["auto_merge_allowed"] is False
    assert r["required_human_approvals"] == 1
    assert classify(["docs/adr/0006-autonomy-scheduling.md"])["tier"] == "T2"


def test_only_current_human_collaborator_approvals_count():
    stale = review("alice", commit="old")
    bot = review("dependabot[bot]", kind="Bot")
    outsider = review("carol", assoc="CONTRIBUTOR")
    self_review = review("author")
    approvers, _ = classify_mod.count_approvals([stale, bot, outsider, self_review], HEAD, "author", PERMISSIONS)
    assert approvers == []


def test_only_approvers_with_write_maintain_or_admin_count():
    # author_association is COLLABORATOR at any permission level, read-only included.
    reviews = [review(login) for login in ("reader", "triager", "writer", "maintainer", "admin", "gone")]
    permissions = {"reader": "read", "triager": "triage", "writer": "write", "maintainer": "maintain",
                   "admin": "admin", "gone": "none"}
    approvers, _ = classify_mod.count_approvals(reviews, HEAD, "author", permissions)
    assert approvers == ["admin", "maintainer", "writer"]


def test_read_only_collaborator_approval_does_not_satisfy_t2():
    r = classify(["research/x.py"])
    approvers, blockers = classify_mod.count_approvals([review("reader")], HEAD, "author", {"reader": "read"})
    assert classify_mod.gate(r, approvers, [], blockers, POLICY)[0] is False
    assert classify_mod.count_approvals([review("unknown")], HEAD, "author", {})[0] == []


def test_latest_review_wins_and_changes_requested_blocks():
    reviews = [review("alice"), review("alice", state="CHANGES_REQUESTED")]
    ok, problems = gate(classify(["research/x.py"]), reviews)
    assert ok is False
    assert any("changes requested" in p for p in problems)
    dismissed = [review("alice"), review("alice", state="DISMISSED")]
    assert classify_mod.count_approvals(dismissed, HEAD, "author", PERMISSIONS)[0] == []


def test_comment_hash_inside_string_survives():
    parsed = json.loads(classify_mod.strip_comments('{"a": "x#y", "b": "q\\"#z"} # note'))
    assert parsed == {"a": "x#y", "b": 'q"#z'}


def test_policy_parses_as_yaml_too():
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load((ROOT / "autonomy" / "POLICY.yaml").read_text()) == POLICY


def test_all_loops_start_disabled():
    assert POLICY["enabled"] is False
    assert all(not loop["enabled"] for loop in POLICY["loops"].values())
    assert [t for t, s in POLICY["tiers"].items() if s["auto_merge_enabled"]] == ["T0"]


def _write_gate_inputs(tmp_path, files, reviews, labels=(), changed=None, permissions=PERMISSIONS):
    pr = {
        "changed_files": len(files) if changed is None else changed,
        "labels": [{"name": l} for l in labels],
        "head": {"sha": HEAD},
        "user": {"login": "author"},
    }
    (tmp_path / "pr.json").write_text(json.dumps(pr))
    (tmp_path / "files.jsonl").write_text("\n".join(json.dumps(f) for f in files))
    (tmp_path / "reviews.jsonl").write_text("\n".join(json.dumps(r) for r in reviews))
    (tmp_path / "permissions.jsonl").write_text(
        "\n".join(json.dumps({"login": k, "permission": v}) for k, v in permissions.items())
    )
    return ["gate", "--pr", str(tmp_path / "pr.json"), "--files", str(tmp_path / "files.jsonl"),
            "--reviews", str(tmp_path / "reviews.jsonl"),
            "--permissions", str(tmp_path / "permissions.jsonl")]


def test_gate_cli_counts_rename_source(tmp_path):
    files = [{"filename": "research/limits_copy.yaml", "previous_filename": "config/limits/prod.yaml"}]
    args = _write_gate_inputs(tmp_path, files, [review("alice")])
    assert classify_mod.main(args) == 1


def test_gate_cli_refuses_truncated_file_list(tmp_path):
    args = _write_gate_inputs(tmp_path, [{"filename": "docs/GLOSSARY.md"}], [], changed=3001)
    assert classify_mod.main(args) == 1


def test_gate_cli_passes_t0(tmp_path):
    args = _write_gate_inputs(tmp_path, [{"filename": "docs/research/graveyard.md"}], [])
    assert classify_mod.main(args) == 0


@pytest.mark.parametrize(
    "path", ["scripts/ci/ai_gate.py", "tests/ci/test_ai_gate.py", "scripts/knowledge/scope_check.py"]
)
def test_checks_the_loops_trust_are_protected_and_never_auto_merge(path):
    # Loosening ai_gate.py turns every paid loop on; loosening scope_check.py widens what a
    # knowledge PR may touch. Both are read from main, so a change to them is T3.
    r = classify([path])
    assert r["tier"] == "T3"
    assert r["auto_merge_allowed"] is False
    loose = json.loads(json.dumps(POLICY))
    loose["protected_paths"] = []
    loose["rules"].insert(0, {"pattern": "**", "tier": "T0"})
    assert classify_mod.classify([path], (), loose)["auto_merge_allowed"] is False


def test_gate_cli_counts_only_write_permission_approvals(tmp_path):
    files = [{"filename": "research/x.py"}]
    assert classify_mod.main(_write_gate_inputs(tmp_path, files, [review("alice")])) == 0
    args = _write_gate_inputs(tmp_path, files, [review("alice")], permissions={"alice": "read"})
    assert classify_mod.main(args) == 1
