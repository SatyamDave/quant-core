#!/usr/bin/env python3
"""Decide whether a paid AI workflow may run, from autonomy/POLICY.yaml. Standard library only.

  ai_gate.py LOOP [--root DIR]

Allows only when all of these hold, and denies otherwise (fail closed: a policy that cannot be
read, or a value that is missing, is a deny):
  - POLICY.yaml "enabled" is true;
  - the repo variable named by "pause_repo_variable" (AUTONOMY_ENABLED), passed in the
    environment, is exactly "true" (unset denies);
  - the pause file named by "pause_file" (autonomy/PAUSE) does not exist;
  - loops[LOOP] exists, is enabled, and has a positive, finite daily_usd, max_turns and
    timeout_minutes.

Prints the reason. Exit 0 allows, 1 denies. When GITHUB_OUTPUT is set, appends run=true|false
and, on allow, max_turns, max_budget_usd and timeout_minutes for the paid step's arguments.
"""

import argparse
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUDGET_KEYS = ("daily_usd", "max_turns", "timeout_minutes")


def strip_comments(text: str) -> str:
    # The one POLICY.yaml parser lives in autonomy/classify.py; reuse it rather than fork it.
    sys.path.insert(0, str(ROOT / "autonomy"))
    from classify import strip_comments as strip

    return str(strip(text))


def decide(loop: str, root: Path, env: dict[str, str]) -> tuple[bool, str, dict[str, float]]:
    try:
        policy = json.loads(strip_comments((root / "autonomy/POLICY.yaml").read_text()))
    except (OSError, ValueError) as e:
        return False, f"cannot read autonomy/POLICY.yaml: {e}", {}
    if policy.get("enabled") is not True:
        return False, "POLICY.yaml enabled is not true", {}
    var = policy.get("pause_repo_variable", "AUTONOMY_ENABLED")
    if env.get(var) != "true":
        return False, f"repo variable {var} is {env.get(var)!r}, not 'true'", {}
    pause = root / policy.get("pause_file", "autonomy/PAUSE")
    if pause.exists():
        return False, f"{pause.relative_to(root)} exists", {}
    entry = policy.get("loops", {}).get(loop)
    if not isinstance(entry, dict):
        return False, f"no loops entry for {loop!r}", {}
    if entry.get("enabled") is not True:
        return False, f"{loop} is not enabled", {}
    budget = {}
    for key in BUDGET_KEYS:
        value = entry.get(key)
        # bool is an int subclass; true must not pass as a budget of 1.
        # json.loads accepts Infinity and NaN, and NaN <= 0 is false.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            return False, f"{loop}.{key} is {value!r}, needs a positive finite number", {}
        budget[key] = value
    return True, f"{loop} allowed", budget


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("loop")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    ok, reason, budget = decide(args.loop, args.root, dict(os.environ))
    print(f"ai-gate: {'allow' if ok else 'deny'}: {reason}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        lines = [f"run={'true' if ok else 'false'}"]
        if ok:
            lines += [
                f"max_turns={int(budget['max_turns'])}",
                f"max_budget_usd={budget['daily_usd']}",
                f"timeout_minutes={int(budget['timeout_minutes'])}",
            ]
        with Path(out).open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
