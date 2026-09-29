#!/usr/bin/env python3
"""Latency bench for issues #31/#55 (wave2/latency): how fast one trading decision really is.

Measures three components of the ADR-0040 decision loop (bridge -> agent -> bridge), using the
REAL qc-bridge release binary and the REAL agent-side TypeScript (BridgeClient, deciders,
runLoop), never a re-implementation of either:

  1. raw bridge round trip  -- a plain Python JSON-Lines client talking directly to qc-bridge's
     stdin/stdout, no TS layer at all. Isolates the bridge/engine's own IPC + book/feature/risk
     overhead from anything Node- or TS-specific.
  2. agent decide step      -- the decider's decide() call alone (fake or replay mode; no network,
     per root CLAUDE.md rule 2 -- this repo makes no live Anthropic call).
  3. full loop              -- next_decision_request -> decide -> submit_order_intent/no_trade,
     exactly as agent/src/loop.ts's runLoop runs it (imported unmodified by
     scripts/bench/decide-bench.mts; this script never edits agent/src or engine/crates/bridge).

Fake and replay mode use the same captured decisions (replay fixtures are generated from the
fake-mode pass, then replayed against a second, identically-configured bridge process -- the
bridge is deterministic, so the same DecisionRequest sequence recurs exactly).

Usage: python3 scripts/bench/latency.py [--n 1000] [--out out/bench/latency-results.json]
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_BIN = REPO_ROOT / "engine" / "target" / "release" / "qc-bridge"
MODEL_SHA_FILE = REPO_ROOT / "ml" / "tests" / "fixtures" / "model.sha256"
HARNESS = REPO_ROOT / "scripts" / "bench" / "decide-bench.mts"
TSX_BIN = REPO_ROOT / "agent" / "node_modules" / ".bin" / "tsx"

DEFAULT_N = 1000
# Protocol v1.1 (schemas/decision/v1): an external-mode approval's expires_ts_ns is the decision
# ts plus this many seconds of market time. This is the TTL the "first HFAT" definition checks
# p99 against.
APPROVAL_TTL_SECONDS = 5.0


# --------------------------------------------------------------------------------------
# Percentile math (issue asks for this to carry its own tests: see test_latency.py)
# --------------------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolation percentile (the "R type 7" / numpy-default method): p in [0, 100].

    Fails closed on bad input rather than silently returning a wrong number -- an empty list or
    an out-of-range p is a caller bug, not a 0.0 or a clamp.
    """
    if not values:
        raise ValueError("percentile() needs at least one value")
    if not (0.0 <= p <= 100.0):
        raise ValueError(f"p must be within [0, 100], got {p}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


@dataclass
class Summary:
    n: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    throughput_per_sec: float


def summarize(values_ms: list[float]) -> Summary:
    n = len(values_ms)
    total_s = sum(values_ms) / 1000.0
    return Summary(
        n=n,
        mean_ms=sum(values_ms) / n,
        p50_ms=percentile(values_ms, 50),
        p95_ms=percentile(values_ms, 95),
        p99_ms=percentile(values_ms, 99),
        max_ms=max(values_ms),
        # Sequential throughput (no pipelining): 1 / mean latency, which is what the loop
        # actually sustains today, not batch-parallel throughput.
        throughput_per_sec=n / total_s if total_s > 0 else float("inf"),
    )


# --------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------


def _cmd_version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(  # noqa: S603 - cmd is a fixed literal argv from this module's callers
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, IndexError):
        return "unavailable"


def _cpu_brand() -> str:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0:
                return out.stdout.strip()
        except OSError:
            pass
    return platform.processor() or "unknown"


def environment_info() -> dict[str, str]:
    git_sha = _cmd_version(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    git_dirty = subprocess.run(  # noqa: S603 - fixed git invocation, no user input
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return {
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "cpu": _cpu_brand(),
        "cpu_count": str(__import__("os").cpu_count()),
        "python": sys.version.split()[0],
        "rustc": _cmd_version(["rustc", "--version"]),
        "node": _cmd_version(["node", "--version"]),
        "git_commit": git_sha,
        "git_dirty": "yes" if git_dirty else "no",
    }


# --------------------------------------------------------------------------------------
# Component 1: raw bridge round trip (plain Python JSON-Lines client, no TS)
# --------------------------------------------------------------------------------------


def bench_raw_bridge(n: int, bridge_args: list[str]) -> list[float]:
    proc = subprocess.Popen(  # noqa: S603 - fixed local BRIDGE_BIN path, argv built from literals
        [str(BRIDGE_BIN), *bridge_args],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("qc-bridge subprocess is missing stdin/stdout pipes")
    timings: list[float] = []
    try:
        for i in range(n):
            req = json.dumps({"v": 1, "id": str(i), "op": "next_decision_request"})
            t0 = time.perf_counter()
            proc.stdin.write(req + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            t1 = time.perf_counter()
            if not line:
                raise RuntimeError(f"qc-bridge closed stdout after {i} raw round trips (need {n})")
            resp = json.loads(line)
            if not resp.get("ok", False):
                raise RuntimeError(f"qc-bridge returned an error: {resp}")
            if resp.get("decision_request") is None:
                raise RuntimeError(
                    f"feed exhausted after {i} raw round trips (need {n}); use a longer recording"
                )
            timings.append((t1 - t0) * 1000.0)
        proc.stdin.write(json.dumps({"v": 1, "id": "shutdown", "op": "shutdown"}) + "\n")
        proc.stdin.flush()
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return timings


# --------------------------------------------------------------------------------------
# Components 2 & 3: agent decide step + full loop (real BridgeClient/Decider/runLoop via the
# Node harness in decide-bench.mts)
# --------------------------------------------------------------------------------------


def run_node_harness(
    mode: str,
    n: int,
    bridge_args: list[str],
    ledger_path: Path,
    fixtures_dir: Path | None = None,
    capture_dir: Path | None = None,
) -> list[dict]:
    cmd = [
        str(TSX_BIN),
        str(HARNESS),
        "--mode",
        mode,
        "--n",
        str(n),
        "--bridge-bin",
        str(BRIDGE_BIN.relative_to(REPO_ROOT)),
        "--bridge-args",
        " ".join(bridge_args),
        "--ledger-path",
        str(ledger_path),
    ]
    if fixtures_dir is not None:
        cmd += ["--fixtures-dir", str(fixtures_dir)]
    if capture_dir is not None:
        cmd += ["--capture-dir", str(capture_dir)]
    result = subprocess.run(  # noqa: S603 - cmd is built above from fixed literals and paths
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"decide-bench.mts ({mode}) failed (exit {result.returncode}):\n{result.stderr}"
        )
    if result.stderr.strip():
        print(f"  [{mode} harness] {result.stderr.strip().splitlines()[-1]}", file=sys.stderr)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def build_bridge() -> None:
    print("building qc-bridge (release)...", file=sys.stderr)
    subprocess.run(
        ["cargo", "build", "--release", "--locked", "-q", "-p", "qc-bridge"],  # noqa: S607
        cwd=REPO_ROOT / "engine",
        check=True,
    )


def require_node_modules() -> None:
    if not TSX_BIN.exists():
        raise SystemExit(f"{TSX_BIN} is missing; run `just agent-setup` (npm ci in agent/) first")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--n", type=int, default=DEFAULT_N, help=f"decisions per component (default {DEFAULT_N})"
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="reuse an already-built qc-bridge release binary"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "out" / "bench" / "latency-results.json",
        help="where to write JSON results",
    )
    args = parser.parse_args()

    if args.n < 1:
        raise SystemExit("--n must be >= 1")

    require_node_modules()
    if not args.skip_build:
        build_bridge()
    if not BRIDGE_BIN.exists():
        raise SystemExit(f"{BRIDGE_BIN} does not exist; run without --skip-build first")

    model_sha = MODEL_SHA_FILE.read_text().strip()
    # --decide-every 1: every synced book update is a decision opportunity, so a 5001-record
    # fixture yields ~4200 decisions -- comfortably above any N this bench is likely to ask for.
    # This is a deliberately dense cadence for bench purposes only; it does not represent a
    # trading policy (see docs/reports/latency.md's "not HFT" section for the real constraint).
    bridge_args = [
        "tests/replay/sample_day.csv",
        "--limits",
        "tests/replay/limits.toml",
        "--decide-every",
        "1",
        "--model",
        "ml/tests/fixtures/model.json",
        "--model-sha256",
        model_sha,
    ]

    out_dir = args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / "tmp"
    fixtures_dir = out_dir / "replay-fixtures"
    for d in (tmp_dir, fixtures_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    print(f"1/3 raw bridge round trip ({args.n} ops)...", file=sys.stderr)
    raw_ms = bench_raw_bridge(args.n, bridge_args)

    print(f"2/3 fake mode: full loop + capture fixtures ({args.n} decisions)...", file=sys.stderr)
    fake_rows = run_node_harness(
        "fake", args.n, bridge_args, tmp_dir / "ledger-fake.jsonl", capture_dir=fixtures_dir
    )

    print(
        f"3/3 replay mode: full loop against captured fixtures ({args.n} decisions)...",
        file=sys.stderr,
    )
    replay_rows = run_node_harness(
        "replay", args.n, bridge_args, tmp_dir / "ledger-replay.jsonl", fixtures_dir=fixtures_dir
    )

    def col(rows: list[dict], key: str) -> list[float]:
        return [r[key] for r in rows]

    components = {
        "raw_bridge_roundtrip": summarize(raw_ms),
        "fake_bridge_request": summarize(col(fake_rows, "bridge_request_ms")),
        "fake_decide": summarize(col(fake_rows, "decide_ms")),
        "fake_bridge_submit": summarize(col(fake_rows, "bridge_submit_ms")),
        "fake_full_loop": summarize(col(fake_rows, "total_ms")),
        "replay_bridge_request": summarize(col(replay_rows, "bridge_request_ms")),
        "replay_decide": summarize(col(replay_rows, "decide_ms")),
        "replay_bridge_submit": summarize(col(replay_rows, "bridge_submit_ms")),
        "replay_full_loop": summarize(col(replay_rows, "total_ms")),
    }

    results = {
        "environment": environment_info(),
        "n_requested": args.n,
        "approval_ttl_seconds": APPROVAL_TTL_SECONDS,
        "components": {k: asdict(v) for k, v in components.items()},
    }
    args.out.write_text(json.dumps(results, indent=2) + "\n")

    print(f"\nwrote {args.out}\n", file=sys.stderr)
    header = (
        f"{'component':<24}{'n':>7}{'mean_ms':>10}{'p50_ms':>10}{'p95_ms':>10}"
        f"{'p99_ms':>10}{'max_ms':>10}{'per_sec':>12}"
    )
    print(header)
    print("-" * len(header))
    for name, s in components.items():
        print(
            f"{name:<24}{s.n:>7}{s.mean_ms:>10.3f}{s.p50_ms:>10.3f}{s.p95_ms:>10.3f}{s.p99_ms:>10.3f}"
            f"{s.max_ms:>10.3f}{s.throughput_per_sec:>12.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
