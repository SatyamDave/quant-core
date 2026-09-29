#!/usr/bin/env python3
"""Classify a PR against autonomy/POLICY.yaml and gate it on approvals.

Standard library only, Python 3.12+ (the ubuntu-24.04 runner's python3).

  classify.py classify [--label L ...] PATH ...
      Print the tier, required approvals and auto-merge eligibility as JSON.
  classify.py gate --pr pr.json --files files.jsonl --reviews reviews.jsonl \
                   --permissions permissions.jsonl
      Classify a PR from GitHub API responses and exit 1 unless it has the
      approvals its tier requires. permissions.jsonl has one
      {"login": ..., "permission": ...} line per approver, from
      repos/{repo}/collaborators/{user}/permission.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

POLICY_PATH = Path(__file__).with_name("POLICY.yaml")
TIER_ORDER = ["T0", "T1", "T2", "T3"]
# Only reviewers who can merge count, mirroring branch protection.
# author_association is COLLABORATOR at any permission level, read included, so
# the repository permission of each approver is checked as well.
COUNTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
COUNTED_PERMISSIONS = {"write", "maintain", "admin"}


def strip_comments(text):
    """Drop '#' comments outside double-quoted strings (POLICY.yaml is JSON plus comments)."""
    out, in_str, escaped, comment = [], False, False, False
    for ch in text:
        if comment:
            if ch == "\n":
                comment = False
                out.append(ch)
            continue
        if in_str:
            if ch == '"' and not escaped:
                in_str = False
            escaped = ch == "\\" and not escaped
        elif ch == '"':
            in_str = True
        elif ch == "#":
            comment = True
            continue
        out.append(ch)
    return "".join(out)


def load_policy(path=POLICY_PATH):
    policy = json.loads(strip_comments(Path(path).read_text()))
    # Fail closed on a malformed policy rather than guess a tier.
    for tier in [policy["default_tier"], *(r["tier"] for r in policy["rules"]), *policy["label_tiers"].values()]:
        if tier not in TIER_ORDER:
            raise ValueError(f"unknown tier {tier!r} in policy")
    for tier in TIER_ORDER:
        policy["tiers"][tier]["human_approvals"]
    return policy


def glob_to_regex(pattern):
    """'**' spans directories ('**/' may match none), '*' and '?' stay in one segment."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def matches(path, patterns):
    return any(glob_to_regex(p).fullmatch(path) for p in patterns)


def path_tier(path, policy):
    if matches(path, policy["protected_paths"]):
        return "T3", "protected path"
    for rule in policy["rules"]:
        if glob_to_regex(rule["pattern"]).fullmatch(path):
            return rule["tier"], f"rule {rule['pattern']}"
    return policy["default_tier"], "no rule matched (default deny)"


def classify(paths, labels=(), policy=None):
    policy = policy or load_policy()
    paths = [p.removeprefix("./") for p in paths]
    tier, reasons = "T0" if paths else policy["default_tier"], []
    if not paths:
        reasons.append("no changed paths (default deny)")
    for path in paths:
        t, why = path_tier(path, policy)
        reasons.append(f"{path}: {t} ({why})")
        tier = max(tier, t, key=TIER_ORDER.index)
    label_tiers = policy["label_tiers"]
    for label in (l.lower() for l in labels):
        if label in label_tiers:
            reasons.append(f"label {label}: {label_tiers[label]}")
            tier = max(tier, label_tiers[label], key=TIER_ORDER.index)
    spec = policy["tiers"][tier]
    auto_merge = spec["auto_merge_enabled"] and not any(
        matches(p, policy["never_auto_merge_paths"]) for p in paths
    )
    return {
        "tier": tier,
        "required_human_approvals": spec["human_approvals"],
        "required_agent_reviews": list(spec["agent_reviews"]),
        "auto_merge_allowed": auto_merge,
        "reasons": reasons,
    }


def count_approvals(reviews, head_sha, author, permissions):
    """Humans with write access or more whose latest review approves the head commit.

    permissions maps login to repository permission; a login missing from it does not count.
    """
    latest = {}
    for r in reviews:  # the API returns reviews oldest first
        if r.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r["user"]["login"]] = r
    approvers, blockers = [], []
    for login, r in latest.items():
        if r["state"] == "CHANGES_REQUESTED":
            blockers.append(login)
        elif (
            r["state"] == "APPROVED"
            and r["user"].get("type") == "User"
            and login != author
            and r.get("author_association") in COUNTED_ASSOCIATIONS
            and permissions.get(login) in COUNTED_PERMISSIONS
            # An approval of an older commit does not cover later pushes.
            and r.get("commit_id") == head_sha
        ):
            approvers.append(login)
    return sorted(approvers), sorted(blockers)


def gate(result, approvers, labels, blockers=(), policy=None):
    policy = policy or load_policy()
    prefix = policy["agent_review_label_prefix"]
    have_agents = {l.lower().removeprefix(prefix) for l in labels if l.lower().startswith(prefix)}
    problems = []
    if len(approvers) < result["required_human_approvals"]:
        problems.append(
            f"{result['tier']} needs {result['required_human_approvals']} human approval(s) "
            f"on the head commit, has {len(approvers)}"
        )
    for agent in result["required_agent_reviews"]:
        if agent not in have_agents:
            problems.append(f"{result['tier']} needs the {agent} agent review (label {prefix}{agent})")
    if blockers:
        problems.append(f"changes requested by {', '.join(blockers)}")
    return not problems, problems


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def run_gate(args):
    policy = load_policy(args.policy)
    pr = json.loads(Path(args.pr).read_text())
    files = read_jsonl(args.files)
    # The files API stops at 3000 entries; an incomplete list could hide a
    # protected path, so refuse to classify it.
    if len(files) != pr["changed_files"]:
        print(f"::error::fetched {len(files)} of {pr['changed_files']} changed files; refusing to classify")
        return 1
    paths = []
    for f in files:
        paths.append(f["filename"])
        # A rename out of a protected zone still touches that zone.
        if f.get("previous_filename"):
            paths.append(f["previous_filename"])
    labels = [l["name"] for l in pr["labels"]]
    result = classify(paths, labels, policy)
    permissions = {p["login"]: p["permission"] for p in read_jsonl(args.permissions)}
    approvers, blockers = count_approvals(
        read_jsonl(args.reviews), pr["head"]["sha"], pr["user"]["login"], permissions
    )
    ok, problems = gate(result, approvers, labels, blockers, policy)

    lines = [
        f"## Autonomy gate: {result['tier']} {'passed' if ok else 'blocked'}",
        f"- Required: {result['required_human_approvals']} human approval(s)"
        + (f", agent review: {', '.join(result['required_agent_reviews'])}" if result["required_agent_reviews"] else ""),
        f"- Approved on head commit by: {', '.join(approvers) or 'nobody'}",
        f"- Auto-merge eligible under policy: {'yes' if result['auto_merge_allowed'] else 'no'}"
        " (Phase A reports only; nothing enables auto-merge yet)",
        *(f"- Blocked: {p}" for p in problems),
        "",
        "<details><summary>Per-path classification</summary>",
        "",
        *(f"- {r}" for r in result["reasons"]),
        "",
        "</details>",
    ]
    report = "\n".join(lines)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(report + "\n")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policy", default=POLICY_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify")
    c.add_argument("--label", action="append", default=[])
    c.add_argument("paths", nargs="*")
    g = sub.add_parser("gate")
    g.add_argument("--pr", required=True)
    g.add_argument("--files", required=True)
    g.add_argument("--reviews", required=True)
    g.add_argument("--permissions", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "classify":
        print(json.dumps(classify(args.paths, args.label, load_policy(args.policy)), indent=2))
        return 0
    return run_gate(args)


if __name__ == "__main__":
    sys.exit(main())
