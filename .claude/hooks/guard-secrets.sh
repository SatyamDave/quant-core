#!/usr/bin/env bash
# PreToolUse (Read|Edit|Write|Bash): block access to secret files and commands that could print secrets.
# Exit 2 blocks the tool call; stderr is the reason Claude sees.
# Python does the JSON parsing so the hook has no jq dependency. Stdin (the hook input) passes through.
set -uo pipefail

code=$(cat <<'PY'
import fnmatch, json, os, re, sys

try:
    data = json.load(sys.stdin)
except ValueError:
    print("guard-secrets: could not parse hook input; blocking to be safe", file=sys.stderr)
    sys.exit(2)

tool = data.get("tool_name", "")
tin = data.get("tool_input") or {}
home = os.path.expanduser("~")

def secret_path(path):
    p = path.replace("\\", "/")
    parts = p.split("/")
    name = parts[-1]
    if fnmatch.fnmatch(name, ".env*"):
        return ".env files hold local credentials"
    if "secrets" in parts[:-1]:
        return "secrets/ directories are never read or written by agents"
    if name.endswith((".pem", ".key")):
        return "private key material"
    for d in (".aws", ".ssh"):
        if p.startswith(f"{home}/{d}/") or p == f"{home}/{d}":
            return f"~/{d} holds credentials"
    return None

# Path-like mentions inside a shell command. A mention must start at a word edge
# (start, space, slash, quote, =, <, >, :) so `process.env.X` and `.venv` do not match.
EDGE = r"(?:^|(?<=[\s/'\"=<>:]))"
BASH_PATTERNS = [
    (EDGE + r"\.env[\w.-]*", 0, ".env files hold local credentials"),
    (r"(?:^|[\s/'\"=<>:])secrets/", 0, "secrets/ directories are never read by agents"),
    (r"[\w-]\.(?:pem|key)\b(?!\.)", 0, "private key material"),
    (r"(?:~|\$HOME|\$\{HOME\}|" + re.escape(home) + r")/\.(?:aws|ssh)\b", 0, "~/.aws and ~/.ssh hold credentials"),
    (r"\bid_(?:rsa|ed25519|ecdsa)\b", 0, "SSH private keys"),
    (r"--api-key\b", 0, "commands carrying --api-key put a credential on the command line"),
    (r"(?:^|[;&|(]\s*)(?:printenv|env|export\s+-p|set)\s*(?:$|[;&|)])", 0, "dumping the environment can print secrets"),
    (r"\$\{?\w*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY)\w*\}?", re.IGNORECASE,
     "expanding a secret-named variable can print it"),
]

reason = None
if tool in ("Read", "Edit", "Write"):
    reason = secret_path(tin.get("file_path", ""))
elif tool == "Bash":
    cmd = tin.get("command", "")
    for pattern, flags, why in BASH_PATTERNS:
        if re.search(pattern, cmd, flags):
            reason = why
            break

if reason:
    print(
        f"Blocked by guard-secrets ({reason}). Rule 2: never read, print, log, or echo secrets. "
        "If you need a credential-shaped value for a test, use an obviously fake literal.",
        file=sys.stderr,
    )
    sys.exit(2)
sys.exit(0)
PY
)

exec python3 -c "$code"
