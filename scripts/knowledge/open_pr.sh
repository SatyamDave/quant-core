#!/usr/bin/env bash
# Open one `knowledge` PR from a patch that a read-only Claude job produced.
# Runs in a job with no Claude step: the scope check is copied from this clean
# checkout before the patch is applied, so a patch cannot rewrite its own check.
# Usage: open_pr.sh <dir with changes.patch and optional pr-body.md> <branch> <title>
set -euo pipefail

dir=$1
branch=$2
title=$3
base=${BASE_BRANCH:-main}

if [ ! -s "$dir/changes.patch" ]; then
  echo "No changes proposed; nothing to open."
  exit 0
fi
# Skip only when an open PR already carries this branch. A branch left by a run whose
# `gh pr create` failed must not silence every later run for the same key.
open_prs=$(gh pr list --head "$branch" --state open --json number --jq 'length')
if [ "${open_prs:-0}" != "0" ]; then
  echo "::notice::An open PR already exists for $branch; not opening a second one."
  exit 0
fi
stale=$(git ls-remote --heads origin "refs/heads/$branch" | cut -f1)

checker=$(mktemp)
cp scripts/knowledge/scope_check.py "$checker"

git switch -c "$branch"
git apply --index "$dir/changes.patch"
git diff --cached --name-only --no-renames | python3 "$checker"

git -c user.name="github-actions[bot]" \
  -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "$title"
if [ -n "$stale" ]; then
  # Replace the leftover branch with this run's scope-checked commit, but only if
  # nobody moved it since ls-remote.
  git push --force-with-lease="refs/heads/$branch:$stale" origin "$branch"
else
  git push origin "$branch"
fi

body=$(mktemp)
if [ -s "$dir/pr-body.md" ]; then cat "$dir/pr-body.md" >"$body"; fi
printf '\n\nProposed by %s run %s/%s/actions/runs/%s. Review before merging; nothing here merges itself.\n' \
  "$GITHUB_WORKFLOW" "$GITHUB_SERVER_URL" "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID" >>"$body"

if ! gh pr create --base "$base" --head "$branch" --title "$title" --body-file "$body" --label knowledge; then
  echo "::error::open_pr.sh could not open the PR for $branch. The branch was pushed and is kept; the next run for this key retries. With GITHUB_TOKEN this fails while 'Allow GitHub Actions to create pull requests' is off." >&2
  exit 1
fi
