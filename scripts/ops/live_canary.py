#!/usr/bin/env python3
"""`just live-canary SYMBOL`: live canary trading of one stock with real orders, as one command.

Refuses (exit 1, before starting anything) unless `scripts/ops/preflight.py`'s every gate passes
for this symbol's instrument file with the OpenRouter decider (ADR-0042), the environment names
the agent and recorder need are set, and neither the kill file nor the supervisor's halt marker
exists. Then, for today's regular session (America/New_York):

  1. the recorder (`agent/src/recorder/cli.ts`, read-only half of the broker adapter) polls your
     broker (QC_BROKER_MODULE) once a second into a fresh data/raw/live/<date>-<HHMMSS>.csv;
  2. ops/live/supervisor.py (halt marker, secret checks, logs in ops/live/logs/) runs the agent
     in openrouter mode, which spawns qc-bridge `--follow --venue external --instrument <file>
     --kill-file <canary kill_file> --decide-every 900` on that recording and executes approved
     orders through the real gateway. `--max-restarts 0`: any agent exit halts rather than
     restarting, because a restarted bridge would re-read the recording from its first line;
  3. this script heartbeats to out/live/<date>/heartbeat.jsonl and raises the recording-stalled
     alert (scripts/ops/alerts.py) during market hours;
  4. at the session close (16:00 ET, or the calendar's early close), or once the halt marker
     appears, it stops everything and runs scripts/ops/daily_close.py for the day.

Touching the kill file (ops/live/state/KILL, config/environments/canary.toml) halts trading
within a second: qc-bridge refuses every new intent and the agent's heartbeat cancels any open
order at the broker. Ctrl-C then stops the processes.

`--reconcile-only` satisfies preflight's reconcile_recent gate before the first start: the agent
in external mode, fake decider, on an empty recording, so it reconciles with the broker (read-only
account, position and order calls) and writes ops/live/state/reconcile-status.json, then exits
with zero decisions. It skips the other gates because it cannot place an order.

`--mock` runs the same preflight gates against a generated fixture tree (every gate satisfied by
fixtures, the kill-switch test stubbed) and then the whole trading chain against the mock
broker and a mock OpenRouter server (agent/src/testkit/run-canary-mock.ts). It
never reads a real credential or opens a network connection beyond localhost.

Exit code: 0 only when preflight passed, no child exited unexpectedly and the daily close was
clean. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
TSX = AGENT_DIR / "node_modules" / ".bin" / "tsx"
HALT_MARKER = ROOT / "ops/live/state/HALT"
LOG_DIR = ROOT / "ops/live/logs"
ET = ZoneInfo("America/New_York")
sys.path.insert(0, str(ROOT / "scripts" / "shadow"))
sys.path.insert(0, str(ROOT / "scripts" / "ops"))
import alerts  # noqa: E402
import new_equity_instrument  # noqa: E402
import preflight  # noqa: E402
import run as shadow  # noqa: E402

# One poll per second across the 6.5 h regular session; check your broker's rate limit, the
# recorder stops rather than exceed this.
DEFAULT_REQUEST_BUDGET = 23_400
# Names only, the same ones preflight.py checks for --decider openrouter.
REQUIRED_ENV = preflight.DECIDERS["openrouter"][1]


MOCK_NOW_NS = 1_790_690_400_000_000_000  # 2026-09-29 10:00 ET, the mock recording's session


def kill_file(root: Path) -> Path:
    return root / tomllib.loads((root / "config/environments/canary.toml").read_text())["kill_file"]


def preflight_gates(
    root: Path,
    instrument: Path,
    env: Mapping[str, str],
    now_ns: int,
    kill_test_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[preflight.GateResult]:
    """Exactly `just preflight-live --decider openrouter --instrument <file>`'s gates."""
    return preflight.run_all_gates(
        root=root,
        instrument_path=instrument,
        approval_path=root / preflight.DEFAULT_APPROVAL,
        policy_path=root / "autonomy/POLICY.yaml",
        reconcile_status=root / preflight.DEFAULT_RECONCILE_STATUS,
        latency_results=root / preflight.DEFAULT_LATENCY_RESULTS,
        max_reconcile_age_min=preflight.DEFAULT_MAX_RECONCILE_AGE_MIN,
        now_ns=now_ns,
        env=env,
        kill_test_cmd=preflight.DEFAULT_KILL_TEST_CMD,
        kill_test_runner=kill_test_runner,
        decider="openrouter",
    )


def write_mock_fixtures(root: Path, symbol: str) -> tuple[Path, dict[str, str]]:
    """A tree where every preflight gate passes, built from the committed configs. Returns the
    instrument file and the environment. SYNTHETIC values only."""
    approval = root / preflight.DEFAULT_APPROVAL
    approval.parent.mkdir(parents=True)
    approval.write_text("SYNTHETIC fixture\n\nApproved by: Example Operator 2026-09-29\n")
    for sub in ("config/environments", "config/limits", "config/instruments/calendars"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    for rel in (
        "config/environments/canary.toml",
        "config/limits/default.toml",
        "config/limits/spy.toml",
        "config/instruments/spy.toml",
        "config/instruments/calendars/nyse.toml",
    ):
        (root / rel).write_bytes((ROOT / rel).read_bytes())
    instrument = new_equity_instrument.write_instrument(symbol, root / "config")
    (root / "autonomy").mkdir()
    policy = (ROOT / "autonomy/POLICY.yaml").read_text()
    disabled = '"loop-agent-openrouter": {"enabled": false, "daily_usd": 0,'
    enabled = '"loop-agent-openrouter": {"enabled": true, "daily_usd": 0.01,'
    if disabled not in policy and enabled not in policy:
        raise RuntimeError("autonomy/POLICY.yaml's loop-agent-openrouter line changed shape")
    (root / "autonomy/POLICY.yaml").write_text(policy.replace(disabled, enabled))
    state = root / preflight.DEFAULT_RECONCILE_STATUS
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"ts_ns": MOCK_NOW_NS - 60 * 10**9, "ok": True}))
    latency = root / preflight.DEFAULT_LATENCY_RESULTS
    latency.parent.mkdir(parents=True)
    latency.write_text(
        json.dumps({"approval_ttl_seconds": 5, "components": {"fake_full_loop": {"p99_ms": 1.0}}})
    )
    env = {
        "OPENROUTER_API_KEY": "SYNTHETIC",
        "QC_OPENROUTER_MODEL": "mock/model:free",
        "QC_BROKER_MODULE": "src/testkit/mock-broker.ts",
    }
    return instrument, env


def session_over_check(
    instrument: Path, clock: Callable[[], int] = time.time_ns
) -> Callable[[], bool]:
    """True once today's session has closed: the market was open and no longer is, or the wall
    clock is past the regular close or on a later day. False while waiting for the open."""
    close = tomllib.loads(instrument.read_text())["trading_hours"]["close"]
    start_day = dt.datetime.fromtimestamp(clock() / 1e9, ET).date()
    seen_open = False

    def check() -> bool:
        nonlocal seen_open
        now = clock()
        if alerts.market_open(instrument, now)[0]:
            seen_open = True
            return False
        local = dt.datetime.fromtimestamp(now / 1e9, ET)
        return seen_open or local.date() != start_day or local.strftime("%H:%M") >= close

    return check


def build_specs(
    instrument: shadow.InstrumentConfig,
    bridge_bin: Path,
    recording: Path,
    out_dir: Path,
    kill: Path,
    request_budget: int,
    env: Mapping[str, str],
) -> list[shadow.ChildSpec]:
    bridge_args = [
        str(recording),
        "--follow",
        "--venue",
        "external",
        "--instrument",
        str(instrument.path),
        "--kill-file",
        str(kill),
        "--decide-every",
        str(shadow.REAL_RUN_DECIDE_EVERY),
    ]
    # agent/src/cli.ts splits QC_BRIDGE_ARGS on spaces.
    if any(" " in a for a in [*bridge_args, str(bridge_bin)]):
        raise ValueError(f"a qc-bridge path contains a space: {bridge_args}")
    agent_env = {
        **env,
        "QC_AGENT_MODE": "openrouter",
        "QC_INSTRUMENT": instrument.symbol,
        "QC_ASSET_CLASS": "equity",
        "QC_BRIDGE_BIN": str(bridge_bin),
        "QC_BRIDGE_ARGS": " ".join(bridge_args),
        "QC_BRIDGE_DECISION_TIMEOUT_MS": "0",  # --follow holds each decision until 900 records
    }
    supervisor_cmd = [
        sys.executable,
        str(ROOT / "ops/live/supervisor.py"),
        "--agent-cmd",
        shlex.join([str(TSX), "src/cli.ts"]),
        *[arg for name in REQUIRED_ENV for arg in ("--require-env", name)],
        "--halt-marker",
        str(HALT_MARKER),
        "--log-dir",
        str(LOG_DIR),
        "--max-restarts",
        "0",
    ]
    recorder_cmd = [
        str(TSX),
        "src/recorder/cli.ts",
        "--instrument-id",
        str(instrument.id),
        "--symbol",
        instrument.symbol,
        "--asset-class",
        "equity",
        "--poll-interval-ms",
        "1000",
        "--request-budget",
        str(request_budget),
        "--out",
        str(recording),
        "--dataset",
        f"live-{recording.stem}",
        "--catalog-dir",
        str(out_dir / "catalog"),
    ]
    return [
        shadow.ChildSpec("recorder", recorder_cmd, AGENT_DIR, dict(env), out_dir / "recorder.log"),
        shadow.ChildSpec(
            "supervisor", supervisor_cmd, AGENT_DIR, agent_env, out_dir / "supervisor.log"
        ),
    ]


def reconcile_only_cmd(
    instrument: shadow.InstrumentConfig, bridge_bin: Path, empty_recording: Path, kill: Path
) -> tuple[list[str], dict[str, str]]:
    bridge_args = [
        str(empty_recording),
        "--venue",
        "external",
        "--instrument",
        str(instrument.path),
    ]
    bridge_args += ["--kill-file", str(kill)]
    env = {
        "QC_AGENT_MODE": "fake",  # there are no records, so it is never asked to decide
        "QC_INSTRUMENT": instrument.symbol,
        "QC_ASSET_CLASS": "equity",
        "QC_BRIDGE_BIN": str(bridge_bin),
        "QC_BRIDGE_ARGS": " ".join(bridge_args),
    }
    return [str(TSX), "src/cli.ts"], env


def run_reconcile_only(instrument: shadow.InstrumentConfig, env: Mapping[str, str]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty.csv"
        empty.touch()
        cmd, extra = reconcile_only_cmd(
            instrument, shadow.build_bridge_binary(), empty, kill_file(ROOT)
        )
        ledger = Path(tmp) / "ledger.jsonl"
        full_env = {**env, **extra, "QC_AGENT_LEDGER_PATH": str(ledger)}
        code = subprocess.run(cmd, cwd=AGENT_DIR, env=full_env, check=False).returncode  # noqa: S603
    print(f"reconcile-only exit={code}; status in {ROOT / preflight.DEFAULT_RECONCILE_STATUS}")
    return code


def start_live(instrument: shadow.InstrumentConfig, args: argparse.Namespace) -> int:
    bridge_bin = shadow.build_bridge_binary()
    now = dt.datetime.now(ET)
    date = now.date().isoformat()
    out_dir = ROOT / "out/live" / date
    # A fresh file per start: qc-bridge --follow reads from the first line, so reusing today's
    # file would replay the morning's quotes as if they were new.
    recording = ROOT / "data/raw/live" / f"{date}-{now:%H%M%S}.csv"
    recording.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    recording.touch()
    specs = build_specs(
        instrument,
        bridge_bin,
        recording,
        out_dir,
        kill_file(ROOT),
        args.request_budget,
        os.environ,
    )
    session_over = session_over_check(instrument.path)
    stopped: list[str] = []

    def should_stop() -> bool:
        if HALT_MARKER.exists():
            stopped.append("halt marker")
        elif session_over():
            stopped.append("session close")
        return bool(stopped)

    print(f"live canary {instrument.symbol} {date}: recording={recording} logs={LOG_DIR}")
    watch = shadow.recording_stall_watch(recording, instrument.path, alerts.DEFAULT_STALL_AFTER_SEC)
    result = shadow.run_children(
        specs, out_dir / "heartbeat.jsonl", on_heartbeat=watch, should_stop=should_stop
    )
    unexpected = [e for e in result.downtime if e.get("event") == "unexpected_exit"]
    children_ok = not unexpected and (result.exit_code == 0 or stopped == ["session close"])
    print(f"stopped by: {stopped[0] if stopped else 'children exited or Ctrl-C'}", flush=True)
    close = subprocess.run(  # noqa: S603 - fixed local script
        [sys.executable, str(ROOT / "scripts/ops/daily_close.py"), "--date", date],
        cwd=ROOT,
        check=False,
    )
    print(f"children_ok={children_ok} daily_close_exit={close.returncode}")
    return 0 if children_ok and close.returncode == 0 else 1


def run_mock(symbol: str, out_dir: Path) -> int:
    return subprocess.run(  # noqa: S603 - fixed local testkit script
        [str(TSX), "src/testkit/run-canary-mock.ts", str(out_dir), symbol],
        cwd=AGENT_DIR,
        check=False,
    ).returncode


def main(
    argv: list[str] | None = None,
    *,
    root: Path = ROOT,
    env: Mapping[str, str] = os.environ,
    now_ns: int | None = None,
    kill_test_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    start: Callable[[shadow.InstrumentConfig, argparse.Namespace], int] = start_live,
) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("symbol")
    p.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET)
    p.add_argument("--mock", action="store_true", help="fixtures + mock broker and model only")
    p.add_argument(
        "--reconcile-only",
        action="store_true",
        help="one read-only reconcile with the broker for preflight's reconcile_recent gate",
    )
    args = p.parse_args(argv)
    try:
        symbol = new_equity_instrument.validate_symbol(args.symbol)
    except ValueError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 1

    mock_dir = None
    if args.mock:
        mock_dir = Path(tempfile.mkdtemp(prefix="live-canary-mock-"))
        root = mock_dir / "root"
        _, env = write_mock_fixtures(root, symbol)
        now_ns = MOCK_NOW_NS

        print("mock: every gate reads SYNTHETIC fixtures; the kill-switch test is stubbed")

        def kill_test_runner(*_a: object, **_k: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess([], 0, "mock: kill-switch test stubbed", "")

    try:
        instrument = shadow.load_instrument(
            symbol, root / "config/instruments" / f"{symbol.lower()}.toml"
        )
    except (ValueError, KeyError, tomllib.TOMLDecodeError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 1

    if args.reconcile_only:
        missing = [n for n in (preflight.BROKER_MODULE_ENV,) if not env.get(n)]
        if missing:
            print(f"refusing: not set (names only): {', '.join(missing)}", file=sys.stderr)
            return 1
        return run_reconcile_only(instrument, env)

    results = preflight_gates(
        root,
        instrument.path,
        env,
        now_ns if now_ns is not None else time.time_ns(),
        kill_test_runner,
    )
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'} {r.name}: {r.detail}")
    failed = [r for r in results if not r.ok]
    if failed:
        print(f"refusing: {len(failed)}/{len(results)} preflight gate(s) failed", file=sys.stderr)
        return 1
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    if missing:
        print(f"refusing: not set (names only): {', '.join(missing)}", file=sys.stderr)
        return 1
    for marker in (kill_file(root), root / "ops/live/state/HALT"):
        if marker.exists():
            print(f"refusing: {marker} exists; read why, then remove it by hand", file=sys.stderr)
            return 1

    if mock_dir is not None:
        print(
            f"mock: preflight satisfied by fixtures in {root}; running the mock chain", flush=True
        )
        return run_mock(symbol, mock_dir / "chain")
    return start(instrument, args)


if __name__ == "__main__":
    sys.exit(main())
