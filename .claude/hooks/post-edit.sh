#!/usr/bin/env bash
# PostToolUse (Edit|Write): format the one changed file and run a fast lint on it.
# A missing tool is skipped silently. Lint findings exit 2 so Claude sees them on stderr;
# the edit has already happened, so this never blocks anything.
set -uo pipefail

input=$(cat)
file=$(python3 -c 'import json,sys; print((json.loads(sys.stdin.read()).get("tool_input") or {}).get("file_path",""))' <<<"$input" 2>/dev/null) || exit 0
[ -n "$file" ] && [ -f "$file" ] || exit 0

root=$(git -C "$(dirname "$file")" rev-parse --show-toplevel 2>/dev/null || echo "${CLAUDE_PROJECT_DIR:-$PWD}")

case "$file" in
  *.rs)
    # rustfmt picks up engine/rustfmt.toml if present; edition matches the workspace.
    command -v rustfmt >/dev/null && rustfmt --edition 2024 "$file" >/dev/null 2>&1
    # Clippy is workspace-wide and too slow for a per-edit hook; `just lint` covers it.
    exit 0
    ;;
  *.py)
    ruff="$root/research/.venv/bin/ruff"
    [ -x "$ruff" ] || ruff=$(command -v ruff) || exit 0
    config=()
    [ -f "$root/research/pyproject.toml" ] && config=(--config "$root/research/pyproject.toml")
    "$ruff" format ${config[@]+"${config[@]}"} --quiet "$file" >/dev/null 2>&1
    if ! out=$("$ruff" check ${config[@]+"${config[@]}"} --quiet "$file" 2>&1); then
      echo "ruff check found issues in $file:" >&2
      echo "$out" >&2
      exit 2
    fi
    ;;
esac
exit 0
