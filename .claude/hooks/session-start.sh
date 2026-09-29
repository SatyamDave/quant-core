#!/usr/bin/env bash
# SessionStart: stdout becomes context for Claude. Keep it short and fast.
set -uo pipefail
cat >/dev/null  # hook input is not needed

root="${CLAUDE_PROJECT_DIR:-$PWD}"
cd "$root" 2>/dev/null || exit 0

branch=$(git branch --show-current 2>/dev/null)
plan=$(ls -t docs/plans/*.md 2>/dev/null | grep -v '/README.md$' | head -1)
inbox=$(find .claude/learnings/inbox -maxdepth 1 -name '*.md' 2>/dev/null | wc -l | tr -d ' ')

echo "quant-core session. Branch: ${branch:-detached or not a git repo}. Active plan: ${plan:-none in docs/plans/}."
echo "Untriaged learnings in .claude/learnings/inbox: ${inbox} (list with 'just learnings')."
echo "Rules 1-3: no LLM or agent calls in the live trading path; no secrets in git and never print them; protected zones (risk, limits, deploy, fund, workflows, .claude hooks/settings, autonomy) are human-approved only."
exit 0
