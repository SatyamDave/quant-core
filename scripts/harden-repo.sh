#!/usr/bin/env bash
# Apply the GitHub settings in docs/runbooks/github-hardening.md.
#
#   scripts/harden-repo.sh                 print every gh api call (dry run)
#   scripts/harden-repo.sh --apply         run them (needs repo admin)
#   scripts/harden-repo.sh --repo o/r ...  target another repository
#
# Safe to re-run: the ruleset is updated in place when it already exists.
set -euo pipefail

repo="${QC_GITHUB_REPO:-OWNER/quant-core}"
apply=false
ruleset_name="main-protection"
# GitHub Actions app id; pins each required check to Actions so another app
# cannot satisfy it by posting a check with the same name.
actions_app_id=15368

# Check-run names of the required jobs (job ids in ci.yml, security.yml and
# autonomy-gate.yml). Keep in sync when a job is renamed.
required_checks=(
  rust
  miri
  python
  just-check
  semgrep
  trivy
  checkov
  "osv-scanner / osv-scan"
  gate
)

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) apply=true ;;
    --repo) repo="$2"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

failures=0

# Prints the call; with --apply also runs it and records a failure without
# stopping, so one unavailable feature does not hide the rest.
call() {
  local desc="$1"; shift
  printf '\n# %s\n' "$desc"
  printf 'gh api'; printf ' %q' "$@"; printf '\n'
  if $apply; then
    if gh api "$@" >/dev/null; then
      echo "ok"
    else
      echo "FAILED: $desc" >&2
      failures=$((failures + 1))
    fi
  fi
}

# Same as call, with a JSON body fed on stdin.
call_json() {
  local desc="$1" body="$2"; shift 2
  printf '\n# %s\n' "$desc"
  printf 'gh api'; printf ' %q' "$@"; printf ' --input - <<JSON\n%s\nJSON\n' "$body"
  if $apply; then
    if printf '%s' "$body" | gh api "$@" --input - >/dev/null; then
      echo "ok"
    else
      echo "FAILED: $desc" >&2
      failures=$((failures + 1))
    fi
  fi
}

checks_json=$(printf '%s\n' "${required_checks[@]}" | python3 -c '
import json, sys
app = int(sys.argv[1])
print(json.dumps([{"context": c, "integration_id": app} for c in sys.stdin.read().splitlines()]))
' "$actions_app_id")

ruleset=$(python3 -c '
import json, sys
print(json.dumps({
    "name": sys.argv[1],
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
    "bypass_actors": [],
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "required_linear_history"},
        {"type": "required_signatures"},
        {"type": "pull_request", "parameters": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews_on_push": True,
            "require_code_owner_review": True,
            "require_last_push_approval": True,
            "required_review_thread_resolution": True,
            "allowed_merge_methods": ["squash", "rebase"],
        }},
        {"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "do_not_enforce_on_create": False,
            "required_status_checks": json.loads(sys.argv[2]),
        }},
    ],
}, indent=2))
' "$ruleset_name" "$checks_json")

$apply && echo "Applying to $repo" || echo "Dry run for $repo (pass --apply to execute)"

call_json "Merge settings: squash or rebase only, delete merged branches" \
  '{"allow_merge_commit": false, "allow_squash_merge": true, "allow_rebase_merge": true, "delete_branch_on_merge": true}' \
  -X PATCH "repos/$repo"

existing_id=""
if $apply; then
  existing_id=$(gh api "repos/$repo/rulesets" --jq ".[] | select(.name == \"$ruleset_name\") | .id" 2>/dev/null || true)
fi
if [ -n "$existing_id" ]; then
  call_json "Update ruleset $ruleset_name on the default branch" "$ruleset" -X PUT "repos/$repo/rulesets/$existing_id"
else
  call_json "Create ruleset $ruleset_name on the default branch (PR + code owner review, stale approvals dismissed, required checks, linear history, no force push or deletion, signed commits)" \
    "$ruleset" -X POST "repos/$repo/rulesets"
fi

call_json "Secret scanning and push protection (private repos need GitHub Secret Protection)" \
  '{"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}}' \
  -X PATCH "repos/$repo"

call "Dependabot alerts" -X PUT "repos/$repo/vulnerability-alerts"
call "Dependabot security updates" -X PUT "repos/$repo/automated-security-fixes"
call "Private vulnerability reporting (public repos only)" -X PUT "repos/$repo/private-vulnerability-reporting"

echo
echo "Manual steps (no API for a user-owned repo): see docs/runbooks/github-hardening.md, 'Manual steps'."

if $apply && [ "$failures" -gt 0 ]; then
  echo "$failures call(s) failed; see the runbook for plan requirements." >&2
  exit 1
fi
