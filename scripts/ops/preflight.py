#!/usr/bin/env python3
"""Go-live preflight for real trading (#50, #51). Standard library only, matching every other
script in scripts/ops and scripts/reports.

Refuses to let live trading start (exit 1, every failing gate printed in plain language) unless
ALL of the following pass. Nothing here is a network call or touches a real broker: this script
only reads files this repository already owns plus environment variable *names*, and runs the
engine's own compiled test binary.

Gates, in the order the spec lists them:
  1. operator_go_live_approval      -- config/go-live/APPROVED.md exists and has at least one
                                        dated "Approved by: <operator> YYYY-MM-DD" line (see
                                        config/go-live/README.md).
  2. instrument_limits_within_canary -- the chosen instrument's config/limits/*.toml exists and
                                        every cap in it is <= the canary environment's own limits.
  3. policy_loop_agent_eval_enabled  -- autonomy/POLICY.yaml's loops.loop-agent-eval is enabled
                                        with a positive daily_usd budget (with --decider
                                        openrouter: policy_loop_agent_openrouter_enabled, the
                                        same check on loops.loop-agent-openrouter, ADR-0042).
  4. kill_file_configured            -- config/environments/canary.toml names a --kill-file path.
  5. kill_switch_test                -- engine/crates/bridge/tests/kill_file_watcher.rs (the real
                                        compiled qc-bridge binary, zero stdin traffic) passes.
  6. reconcile_recent                -- the last recorded reconcile against the broker was clean
                                        and within --max-reconcile-age-min.
  7. required_secrets_present        -- the secret *names* ops/live/bin/load-secrets.sh expects,
                                        plus QC_BROKER_MODULE (your broker adapter), are set in
                                        the environment (values are never read here).
  8. latency_p99_under_approval_ttl  -- the last latency bench's full-loop p99 is under protocol
                                        v1.1's approval-stamp TTL (out/bench/latency-results.json).

Usage:
    scripts/ops/preflight.py                      # (also `just preflight-live`)
    scripts/ops/preflight.py --instrument config/instruments/spy.toml
    scripts/ops/preflight.py --decider openrouter --instrument config/instruments/f.toml

Exit code: 0 if every gate passes, 1 otherwise (never 2 -- a gate that could not even be checked
is a failing gate, not a skip: "a check that could not run is not a check that passed").
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# The example instrument config. Pass --instrument for the one you actually trade.
DEFAULT_INSTRUMENT = Path("config/instruments/spy.toml")

# The operator's written go-live approval. Not committed by default, so this gate fails closed
# until a named operator decides to go live (config/go-live/README.md).
DEFAULT_APPROVAL = Path("config/go-live/APPROVED.md")

# Caps where "instrument value <= canary value" is unambiguously "at least as tight," in units that
# are comparable across instruments. Left out on purpose:
#   - max_position: a raw quantity in the instrument's own unit (config/limits/default.toml's own
#     comment: "e.g. 0.01 BTC"; config/limits/spy.toml: whole shares) -- 10 SPY shares vs 0.01 BTC
#     is not a real violation, it's two different units compared as if they were one.
#   - price_band_bps / stale_data_ms: tighter is a *smaller* value, same direction as these, but
#     not a spend/exposure cap the "within canary caps" language is about.
#   - wash_trade_window_ms: tighter detection is a *larger* window, the opposite direction --
#     comparing it here would flag a stricter instrument config as a violation.
CAP_KEYS = (
    "max_notional",
    "max_order_rate_per_sec",
    "max_daily_loss",
    "max_daily_notional",
)

# ops/live/bin/load-secrets.sh is the one place these names are decided; this list must track
# that file, not invent a second list. QC_BROKER_MODULE is not a secret: it names the broker
# adapter module (agent/src/broker/README.md), which loads its own credentials, and without it
# agent/src/cli.ts refuses to start external mode.
BROKER_MODULE_ENV = "QC_BROKER_MODULE"
REQUIRED_SECRET_NAMES = ("ANTHROPIC_API_KEY", BROKER_MODULE_ENV)

# --decider -> (the autonomy/POLICY.yaml loop that must be enabled, the env names that must be
# set). The OpenRouter decider (ADR-0042) never reads ANTHROPIC_API_KEY; it needs its own key and
# model id instead (agent/src/decider/openrouter.ts's resolveOpenRouterConfig).
DECIDERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "live": ("loop-agent-eval", REQUIRED_SECRET_NAMES),
    "openrouter": (
        "loop-agent-openrouter",
        ("OPENROUTER_API_KEY", "QC_OPENROUTER_MODEL", BROKER_MODULE_ENV),
    ),
}

# Written after every reconcile pass by agent/src/broker/reconcile.ts (default path in
# agent/src/paths.ts's reconcileStatusPath(), overridable with QC_RECONCILE_STATUS_PATH).
DEFAULT_RECONCILE_STATUS = Path("ops/live/state/reconcile-status.json")
DEFAULT_LATENCY_RESULTS = Path("out/bench/latency-results.json")
DEFAULT_MAX_RECONCILE_AGE_MIN = 60.0
DEFAULT_KILL_TEST_CMD = (
    "cargo",
    "test",
    "--locked",
    "-p",
    "qc-bridge",
    "--test",
    "kill_file_watcher",
)

MINUTE_NS = 60_000_000_000
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    detail: str


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def _gate(name: str, fn: Callable[[], GateResult]) -> GateResult:
    """A gate that raises (malformed TOML, cargo/the binary missing, a permissions error) has not
    passed -- root CLAUDE.md's "a check that could not run is not a check that passed" applies to
    this tool's own gates, not just the ones it reads about. Converts any exception to a clean
    FAIL instead of a stack trace and a non-1 exit code."""
    try:
        return fn()
    except Exception as exc:  # deliberately catch-all; see docstring
        return GateResult(name, False, f"unexpected error while checking: {exc!r}")


def _load_classify_module():
    """autonomy/classify.py already parses POLICY.yaml's JSON-plus-comments format (its own
    docstring: readable "with the Python standard library alone") -- reuse that parser rather than
    re-implementing it here. Always this repository's own classify.py (a generic parser, not
    something that varies with the --policy file under test), so a test can point --policy at a
    fixture without needing a fixture autonomy/ directory too."""
    path = REPO_ROOT / "autonomy" / "classify.py"
    spec = importlib.util.spec_from_file_location("_preflight_classify", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {path} as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_preflight_classify"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------------------


def check_go_live_approval(path: Path) -> GateResult:
    """Every "Approved by:" line must name an operator and carry a YYYY-MM-DD date; at least one
    such line must exist."""
    name = "operator_go_live_approval"
    if not path.is_file():
        return GateResult(name, False, f"{path} does not exist (see {path.parent}/README.md)")
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().lower().startswith("approved by:")
    ]
    if not lines:
        return GateResult(name, False, f"{path} has no 'Approved by:' line")
    for line in lines:
        if not DATE_RE.search(line):
            return GateResult(name, False, f"{path} has an undated 'Approved by:' line: {line!r}")
        operator = DATE_RE.sub("", line.split(":", 1)[1]).strip(" -,()")
        if not operator:
            return GateResult(name, False, f"{path} has an 'Approved by:' line with no operator")
    return GateResult(name, True, f"{path} has {len(lines)} dated operator approval(s)")


def check_instrument_limits(root: Path, instrument_path: Path) -> GateResult:
    name = "instrument_limits_within_canary"
    if not instrument_path.is_file():
        return GateResult(name, False, f"{instrument_path} does not exist")
    instrument = load_toml(instrument_path)
    instrument_limits_rel = instrument.get("limits")
    if not instrument_limits_rel:
        return GateResult(name, False, f"{instrument_path} has no 'limits' key")
    instrument_limits_path = (instrument_path.parent / instrument_limits_rel).resolve()
    if not instrument_limits_path.is_file():
        return GateResult(
            name,
            False,
            f"{instrument_limits_path} ({instrument_path}'s 'limits' key) does not exist",
        )

    canary_path = root / "config/environments/canary.toml"
    if not canary_path.is_file():
        return GateResult(name, False, f"{canary_path} does not exist")
    canary_env = load_toml(canary_path)
    canary_limits_rel = canary_env.get("limits")
    if not canary_limits_rel:
        return GateResult(name, False, f"{canary_path} has no 'limits' key")
    canary_limits_path = (root / canary_limits_rel).resolve()
    if not canary_limits_path.is_file():
        return GateResult(
            name, False, f"{canary_limits_path} (canary's 'limits' key) does not exist"
        )

    instrument_limits = load_toml(instrument_limits_path)
    canary_limits = load_toml(canary_limits_path)

    violations = []
    for key in CAP_KEYS:
        if key not in instrument_limits or key not in canary_limits:
            continue
        try:
            instrument_value = Decimal(str(instrument_limits[key]))
            canary_value = Decimal(str(canary_limits[key]))
        except InvalidOperation:
            violations.append(f"{key}: not a comparable number")
            continue
        if instrument_value > canary_value:
            violations.append(f"{key}: instrument={instrument_value} > canary cap={canary_value}")
    if violations:
        return GateResult(
            name,
            False,
            f"{instrument_limits_path} exceeds canary caps in {canary_limits_path}: "
            + "; ".join(violations),
        )
    return GateResult(
        name, True, f"{instrument_limits_path} is within canary caps ({canary_limits_path})"
    )


def check_policy_agent_eval(policy_path: Path, loop_name: str = "loop-agent-eval") -> GateResult:
    name = f"policy_{loop_name.replace('-', '_')}_enabled"
    try:
        classify = _load_classify_module()
        policy = classify.load_policy(policy_path)
    except Exception as exc:  # fail closed on any parse error, not just the expected ones
        return GateResult(name, False, f"could not parse {policy_path}: {exc}")
    loop = policy.get("loops", {}).get(loop_name)
    if loop is None:
        return GateResult(name, False, f"{policy_path} has no loops.{loop_name} entry")
    if not loop.get("enabled"):
        return GateResult(name, False, f"autonomy/POLICY.yaml loops.{loop_name}.enabled is false")
    daily_usd = loop.get("daily_usd", 0)
    is_positive_number = (
        isinstance(daily_usd, (int, float)) and not isinstance(daily_usd, bool) and daily_usd > 0
    )
    if not is_positive_number:
        return GateResult(
            name,
            False,
            f"autonomy/POLICY.yaml loops.{loop_name}.daily_usd is {daily_usd!r}, "
            "needs a positive budget",
        )
    return GateResult(name, True, f"{loop_name} enabled with daily_usd={daily_usd}")


def check_kill_file_configured(root: Path) -> GateResult:
    name = "kill_file_configured"
    canary_path = root / "config/environments/canary.toml"
    if not canary_path.is_file():
        return GateResult(name, False, f"{canary_path} does not exist")
    kill_file = load_toml(canary_path).get("kill_file")
    if not kill_file:
        return GateResult(name, False, f"{canary_path} has no non-empty 'kill_file' key")
    return GateResult(name, True, f"canary kill_file configured at {kill_file!r}")


def check_kill_switch_test(
    root: Path,
    cmd: tuple[str, ...] = DEFAULT_KILL_TEST_CMD,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> GateResult:
    name = "kill_switch_test"
    result = runner(list(cmd), cwd=root / "engine", capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-10:])
        return GateResult(name, False, f"`{' '.join(cmd)}` exited {result.returncode}:\n{tail}")
    return GateResult(name, True, f"`{' '.join(cmd)}` passed")


def check_reconcile_recent(
    root: Path, status_path: Path, now_ns: int, max_age_min: float
) -> GateResult:
    name = "reconcile_recent"
    path = status_path if status_path.is_absolute() else root / status_path
    if not path.is_file():
        return GateResult(
            name, False, f"{path} does not exist -- no reconcile has been recorded yet"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return GateResult(name, False, f"{path} is not valid JSON: {exc}")
    ts_ns = data.get("ts_ns")
    if not isinstance(ts_ns, int):
        return GateResult(name, False, f"{path} has no integer 'ts_ns'")
    if data.get("ok") is not True:
        return GateResult(
            name, False, f"{path}'s last reconcile was not clean: {data.get('discrepancies', data)}"
        )
    age_min = (now_ns - ts_ns) / MINUTE_NS
    if age_min < 0:
        return GateResult(name, False, f"{path}'s ts_ns is in the future")
    if age_min > max_age_min:
        return GateResult(
            name,
            False,
            f"last clean reconcile was {age_min:.1f} min ago, "
            f"older than the {max_age_min:g} min limit",
        )
    return GateResult(name, True, f"last clean reconcile was {age_min:.1f} min ago")


def check_secrets_present(
    env: Mapping[str, str], names: tuple[str, ...] = REQUIRED_SECRET_NAMES
) -> GateResult:
    name = "required_secrets_present"
    missing = [n for n in names if not env.get(n)]
    if missing:
        return GateResult(
            name, False, f"missing required secret(s) (names only): {', '.join(missing)}"
        )
    return GateResult(name, True, f"present: {', '.join(names)}")


def check_latency_under_ttl(root: Path, results_path: Path) -> GateResult:
    name = "latency_p99_under_approval_ttl"
    path = results_path if results_path.is_absolute() else root / results_path
    if not path.is_file():
        return GateResult(name, False, f"{path} does not exist -- run `just bench-latency` first")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return GateResult(name, False, f"{path} is not valid JSON: {exc}")
    ttl_seconds = data.get("approval_ttl_seconds")
    components = data.get("components", {})
    full_loop = components.get("fake_full_loop") or components.get("replay_full_loop")
    if ttl_seconds is None or not isinstance(full_loop, dict) or "p99_ms" not in full_loop:
        return GateResult(
            name, False, f"{path} is missing 'approval_ttl_seconds' or a full-loop 'p99_ms'"
        )
    ttl_ms = float(ttl_seconds) * 1000
    p99_ms = float(full_loop["p99_ms"])
    if p99_ms >= ttl_ms:
        return GateResult(
            name,
            False,
            f"full-loop p99 {p99_ms:.3f}ms is not under the {ttl_ms:.0f}ms approval TTL",
        )
    return GateResult(
        name, True, f"full-loop p99 {p99_ms:.3f}ms is under the {ttl_ms:.0f}ms approval TTL"
    )


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def run_all_gates(
    root: Path,
    instrument_path: Path,
    approval_path: Path,
    policy_path: Path,
    reconcile_status: Path,
    latency_results: Path,
    max_reconcile_age_min: float,
    now_ns: int,
    env: Mapping[str, str],
    kill_test_cmd: tuple[str, ...] = DEFAULT_KILL_TEST_CMD,
    kill_test_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    decider: str = "live",
) -> list[GateResult]:
    policy_loop, secret_names = DECIDERS[decider]
    results = [check_go_live_approval(approval_path)]
    results.append(
        _gate(
            "instrument_limits_within_canary",
            lambda: check_instrument_limits(root, instrument_path),
        )
    )
    results.append(check_policy_agent_eval(policy_path, policy_loop))
    results.append(_gate("kill_file_configured", lambda: check_kill_file_configured(root)))
    results.append(
        _gate(
            "kill_switch_test",
            lambda: check_kill_switch_test(root, kill_test_cmd, kill_test_runner),
        )
    )
    results.append(check_reconcile_recent(root, reconcile_status, now_ns, max_reconcile_age_min))
    results.append(check_secrets_present(env, secret_names))
    results.append(check_latency_under_ttl(root, latency_results))
    return results


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", type=Path, default=REPO_ROOT)
    p.add_argument("--instrument", type=Path, default=DEFAULT_INSTRUMENT)
    p.add_argument("--decider", choices=sorted(DECIDERS), default="live")
    p.add_argument("--approval", type=Path, default=DEFAULT_APPROVAL)
    p.add_argument("--policy", type=Path, default=None, help="default: <root>/autonomy/POLICY.yaml")
    p.add_argument("--reconcile-status", type=Path, default=DEFAULT_RECONCILE_STATUS)
    p.add_argument("--latency-results", type=Path, default=DEFAULT_LATENCY_RESULTS)
    p.add_argument("--max-reconcile-age-min", type=float, default=DEFAULT_MAX_RECONCILE_AGE_MIN)
    p.add_argument("--now-ns", type=int, help="override 'now' for deterministic testing")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    instrument_path = args.instrument if args.instrument.is_absolute() else root / args.instrument
    approval_path = args.approval if args.approval.is_absolute() else root / args.approval
    policy_path = args.policy if args.policy is not None else root / "autonomy/POLICY.yaml"
    now_ns = args.now_ns if args.now_ns is not None else time.time_ns()

    results = run_all_gates(
        root=root,
        instrument_path=instrument_path,
        approval_path=approval_path,
        policy_path=policy_path,
        reconcile_status=args.reconcile_status,
        latency_results=args.latency_results,
        max_reconcile_age_min=args.max_reconcile_age_min,
        now_ns=now_ns,
        env=os.environ,
        decider=args.decider,
    )

    failed = [r for r in results if not r.ok]
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'} {r.name}: {r.detail}")

    if failed:
        print(f"\npreflight: {len(failed)}/{len(results)} gate(s) failed. Not clear to go live.")
        return 1
    print(f"\npreflight: all {len(results)} gates passed. Clear to go live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
