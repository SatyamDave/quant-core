#!/usr/bin/env bash
# PreToolUse (Edit|Write): block edits to protected zones (root CLAUDE.md rule 3).
# QC_ALLOW_PROTECTED=1 in the environment Claude Code was launched with unlocks them,
# except that a change loosening a numeric limit in config/limits/*.toml stays blocked (rule 4).
# Exit 2 blocks the tool call. Anything this hook cannot evaluate is blocked, not allowed.
set -uo pipefail

code=$(cat <<'PY'
import fnmatch, json, os, re, subprocess, sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Matched case-insensitively: on macOS and Windows Config/Limits/x.toml is config/limits/x.toml.
PROTECTED = [
    "engine/crates/risk/**",
    "config/limits/**",
    "ops/deploy/**",
    "fund/**",
    ".github/workflows/**",
    ".github/CODEOWNERS",
    ".claude/hooks/**",
    ".claude/settings*.json",
    ".claude/agents/risk-auditor.md",
    "autonomy/**",
    "tests/autonomy/**",
    "tests/hooks/**",
    "tests/ci/**",
    "scripts/ci/ai_gate.py",
    "scripts/knowledge/scope_check.py",
]
# Keys where a larger value is a looser limit. min_* is the reverse. Any other numeric key
# has no known direction, so any change to it is treated as loosening.
LARGER_IS_LOOSER = re.compile(r"^(max_\w+|price_band_bps|stale_data_ms)$")
SMALLER_IS_LOOSER = re.compile(r"^min_\w+$")


def block(msg):
    print(f"Blocked by guard-protected: {msg}", file=sys.stderr)
    sys.exit(2)


def main():
    data = json.load(sys.stdin)
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    file_path = Path(tin.get("file_path", "").replace("\\", "/"))
    if not file_path.is_absolute():
        file_path = Path(data.get("cwd") or os.getcwd()) / file_path

    def repo_root(path):
        probe = path.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            out = subprocess.run(
                ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0:
                return Path(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
        return Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())

    root = repo_root(file_path)
    target = os.path.normpath(str(file_path.resolve()))
    base = os.path.normpath(str(root.resolve()))
    if not target.casefold().startswith(base.casefold() + os.sep):
        sys.exit(0)  # outside the repository: not a protected zone
    rel = Path(target[len(base) + 1:]).as_posix().casefold()

    zone = next((z for z in PROTECTED if fnmatch.fnmatchcase(rel, z.casefold())), None)
    if zone is None:
        sys.exit(0)

    if os.environ.get("QC_ALLOW_PROTECTED") != "1":
        block(
            f"{rel} is in protected zone {zone}. Protected zones are human-approved only. "
            "Propose the change in the PR description instead, or ask the human to restart "
            "the session with QC_ALLOW_PROTECTED=1."
        )

    if not rel.startswith("config/limits/"):
        sys.exit(0)
    if not rel.endswith(".toml"):
        block(f"{rel}: only .toml files belong in config/limits/, and the limit check reads only those.")

    import tomllib  # stdlib since 3.11

    old_text = file_path.read_text() if file_path.exists() else None
    if tool == "Write":
        new_text = tin["content"]
        if not isinstance(new_text, str):
            block(f"Write content for {rel} is not text.")
    elif tool == "Edit" and old_text is not None:
        old_s, new_s = tin.get("old_string", ""), tin.get("new_string", "")
        if not old_s or old_s not in old_text:
            block(f"cannot compute the new contents of {rel} (old_string not found), so the limit check cannot run.")
        new_text = old_text.replace(old_s, new_s) if tin.get("replace_all") else old_text.replace(old_s, new_s, 1)
    else:
        block(f"unexpected tool {tool} on {rel}.")

    def leaves(text, label):
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            block(f"{label} contents of {rel} are not valid TOML ({e}); the limit check cannot run.")
        out = {}

        def walk(node, prefix):
            for k, v in node.items():
                key = f"{prefix}{k}"
                if isinstance(v, dict):
                    walk(v, key + ".")
                    continue
                value = None
                if isinstance(v, (int, float, str)) and not isinstance(v, bool):
                    try:
                        value = Decimal(str(v))
                    except InvalidOperation:
                        pass
                if value is None or not value.is_finite():
                    block(f"{label} {key} = {v!r} in {rel} is not a finite number; the limit check cannot compare it.")
                out[key] = value

        walk(doc, "")
        return out

    # A new file is compared with default.toml: a strategy file may only tighten the defaults.
    default_path = root / "config" / "limits" / "default.toml"
    if not default_path.is_file():
        block("config/limits/default.toml is missing, so there is no baseline to compare limits with.")
    defaults = leaves(default_path.read_text(), "default")
    old_vals = leaves(old_text, "current") if old_text is not None else {}
    new_vals = leaves(new_text, "proposed")

    loosened = [f"{key} removed (was {old})" for key, old in old_vals.items() if key not in new_vals]
    for key, new in new_vals.items():
        old = old_vals.get(key, defaults.get(key))
        leaf = key.rsplit(".", 1)[-1]
        if old is None:
            loosened.append(f"{key} is not a key in config/limits/default.toml")
        elif LARGER_IS_LOOSER.match(leaf):
            if new > old:
                loosened.append(f"{key} raised {old} -> {new}")
        elif SMALLER_IS_LOOSER.match(leaf):
            if new < old:
                loosened.append(f"{key} lowered {old} -> {new}")
        elif new != old:
            loosened.append(f"{key} changed {old} -> {new} (no known tightening direction for this key)")

    if loosened:
        block(
            "this change loosens risk limits in " + rel + ": " + "; ".join(loosened) + ". "
            "Rule 4: limits are only tightened automatically; loosening needs two human approvals, "
            "even with QC_ALLOW_PROTECTED=1."
        )


try:
    main()
except SystemExit:
    raise
except Exception as e:
    block(f"the hook could not evaluate this call ({e.__class__.__name__}: {e}), so it is blocked.")
sys.exit(0)
PY
)

python3 -c "$code"
rc=$?
# Any failure to evaluate (no python3, a crash) blocks; only a clean 0 allows.
if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
  echo "Blocked by guard-protected: the hook could not run (exit $rc), so it is blocked." >&2
fi
[ "$rc" -eq 0 ] || exit 2
