#!/usr/bin/env python3
"""Issue #48 (A5 Shadow, infrastructure): one command that runs the agent in shadow mode --
watching real/live prices and deciding every time, with every order routed only to the
simulated venue, never to a real broker.

  scripts/shadow/run.py [--dry-run] [--date YYYY-MM-DD] [--decider fake|live|openrouter]
      [--symbol SPY] [...]
  just shadow [--dry-run] [--decider openrouter --symbol F]

Starts four things for one calendar day's session (`--date`, default: today UTC):
  1. the recorder (`agent/src/recorder/cli.ts`; the mock broker in `--dry-run`, your broker
     adapter via QC_BROKER_MODULE otherwise -- see agent/src/broker/README.md), writing to
     data/raw/shadow/<date>.csv;
  2. the real `qc-bridge` binary in `--follow --venue sim` mode on that same, still-growing file
     (protocol v1.2, `engine/crates/bridge/src/feed.rs`), with `--instrument` naming
     config/instruments/<symbol>.toml (tick, whole shares, trading hours, and that file's own
     limits) -- every decision is simulated, never routed to a real venue;
  3. the agent service (`agent/src/cli.ts`) with the `--decider` mode: fake (default), live
     (Claude, gated by `scripts/ci/ai_gate.py loop-agent-eval`) or openrouter (ADR-0042, gated by
     `loop-agent-openrouter`). Both gates are disabled with a zero budget in autonomy/POLICY.yaml
     until a maintainer enables one, so those modes refuse; this script never falls back to fake
     silently when the operator asked for a model (`--live` is kept as `--decider live`);
  4. this script's own heartbeat: a liveness line every few seconds to
     out/shadow/<date>/heartbeat.jsonl, and a distinct event the moment one child exits
     unexpectedly (non-zero) while the other is still running -- issue #48's "any downtime
     documented." -- and a `recording_stalled` event (plus a console alert, via
     scripts/ops/alerts.py's own check) when the recording stops growing during market hours.

Writes out/shadow/<date>/ledger.jsonl (the decision ledger) and, once both children have
exited, a daily report via scripts/reports/{ledger,pnl}_report.py (forward metrics vs no-trade
and buy-and-hold) under out/shadow/<date>/report/ (gitignored) -- never a committed path, because a
broker-sourced trade record is reconciled separately (root CLAUDE.md rule 10); a shadow run is
simulated, and must never be mistaken for it. The manifest the recorder writes also goes under
out/shadow/<date>/catalog/ rather than the committed data/catalog/, for the same reason: a
shadow day's dataset is a run artifact, not a permanent research dataset.

Exit code: 0 only if both children exited zero (the pnl report's own stop-rule exit code is
surfaced but never folded in here -- a tripped stop rule is a finding for whoever reads the
report, not a failure of this script). Standard library only.

The agent runs with QC_BRIDGE_DECISION_TIMEOUT_MS=0: qc-bridge --follow holds each
next_decision_request answer until the growing recording yields a decision, which in a quiet
market can take longer than BridgeClient's default 5s, so that one op waits indefinitely. Every
other op keeps its bounded timeout, and a bridge that exits still fails the agent at once (its
exit handler rejects the pending call), which this script's heartbeat records as downtime.

A real multi-day run needs an external scheduler (cron/systemd) to invoke this script once per
day (`--date` rolls the log/report files over); it is not itself a long-running daemon.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
INSTRUMENTS_DIR = ROOT / "config" / "instruments"
# --decider -> (QC_AGENT_MODE, the scripts/ci/ai_gate.py loop that must allow it, or None).
DECIDERS = {
    "fake": ("fake", None),
    "live": ("live", "loop-agent-eval"),
    "openrouter": ("openrouter", "loop-agent-openrouter"),
}
# One decision per 900 synced records: ~15 min at the recorder's 1 s poll, ~26 per 6.5 h session,
# so a free model's daily request limit is not exceeded. qc-bridge's own default (50) is left
# alone; ops/live/live.env.example passes the same value.
REAL_RUN_DECIDE_EVERY = 900

# Reuses scripts/eval/{bridge_sessions,live_agent}.py's own cargo-build and ai_gate.py helpers
# (same binary path, same loop name, same fail-closed contract) instead of a second
# implementation of either -- both are read-only, side-effect-free to import (each guarded by
# `if __name__ == "__main__":`).
sys.path.insert(0, str(ROOT / "scripts" / "eval"))
import bridge_sessions  # noqa: E402
import live_agent  # noqa: E402

sys.path.insert(0, str(ROOT / "scripts" / "ops"))
import alerts  # noqa: E402

DEFAULT_HEARTBEAT_INTERVAL_SEC = 2.0
# Bounded, so a stuck child can't hang `just shadow` forever once a stop was requested.
STOP_TIMEOUT_SEC = 5.0


def today_utc() -> str:
    return dt.datetime.now(dt.UTC).date().isoformat()


@dataclass
class ShadowPaths:
    date: str
    recording: Path
    out_dir: Path
    ledger: Path
    heartbeat_log: Path
    catalog_dir: Path
    report_dir: Path


def paths_for(date: str, root: Path = ROOT) -> ShadowPaths:
    out_dir = root / "out" / "shadow" / date
    return ShadowPaths(
        date=date,
        recording=root / "data" / "raw" / "shadow" / f"{date}.csv",
        out_dir=out_dir,
        ledger=out_dir / "ledger.jsonl",
        heartbeat_log=out_dir / "heartbeat.jsonl",
        catalog_dir=out_dir / "catalog",
        report_dir=out_dir / "report",
    )


def check_live_gate(root: Path = ROOT, loop: str = "loop-agent-eval") -> tuple[bool, str]:
    """scripts/eval/live_agent.py's own ai_gate.py check for `loop` (read-only, fail closed on
    any error running it)."""
    return live_agent.gate_allows(root, loop)


@dataclass(frozen=True)
class InstrumentConfig:
    path: Path
    id: int
    symbol: str


def load_instrument(symbol: str, path: Path | None = None) -> InstrumentConfig:
    """config/instruments/<symbol>.toml (or `path`), checked to be the same symbol. qc-bridge's
    order book rejects a record whose instrument id differs from its config's, so the recorder
    takes its id from here rather than from a second flag."""
    path = (path or INSTRUMENTS_DIR / f"{symbol.lower()}.toml").resolve()
    if not path.is_file():
        raise ValueError(
            f"{path} does not exist; create it with `just new-equity-instrument {symbol}`"
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("symbol") != symbol:
        raise ValueError(f"{path} is for symbol {data.get('symbol')!r}, not {symbol!r}")
    return InstrumentConfig(path=path, id=int(data["id"]), symbol=symbol)


def build_bridge_binary(root: Path = ROOT) -> Path:
    """scripts/eval/bridge_sessions.py's own cargo-build helper. Raises rather than silently
    falling back to a stale or missing binary."""
    built, detail = bridge_sessions.ensure_bridge_built(root)
    if not built:
        raise RuntimeError(detail)
    return root / "engine" / "target" / "release" / "qc-bridge"


@dataclass
class ChildSpec:
    name: str
    cmd: list[str]
    cwd: Path
    env: dict[str, str]
    log_path: Path


@dataclass
class RunResult:
    exit_code: int
    downtime: list[dict[str, object]] = field(default_factory=list)


def _write_line(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True) + "\n")


def recording_stall_watch(
    recording: Path,
    instrument: Path,
    stall_after_sec: float,
    clock_ns: Callable[[], int] = time.time_ns,
) -> Callable[[], list[dict[str, object]]]:
    """A per-heartbeat check that returns one `recording_stalled` event when the recording
    starts stalling (not on every heartbeat while it stays stalled). The first stall_after_sec
    after the watch starts is a grace period: the recorder has not written its first row yet."""
    started_ns = clock_ns()
    stalled = False
    notifier = alerts.ConsoleNotifier()

    def check() -> list[dict[str, object]]:
        nonlocal stalled
        now_ns = clock_ns()
        if now_ns - started_ns < stall_after_sec * alerts.SECOND_NS:
            return []
        found = alerts.check_recording_stalled(recording, instrument, now_ns, stall_after_sec)
        was_stalled, stalled = stalled, bool(found)
        if not found or was_stalled:
            return []
        notifier.send(found[0])
        return [
            {
                "ts": dt.datetime.now(dt.UTC).isoformat(),
                "event": "recording_stalled",
                "message": found[0].message,
            }
        ]

    return check


def run_children(
    specs: list[ChildSpec],
    heartbeat_log: Path,
    poll_interval_sec: float = DEFAULT_HEARTBEAT_INTERVAL_SEC,
    on_heartbeat: Callable[[], list[dict[str, object]]] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunResult:
    """Starts every child, heartbeats (poll + sleep, never busy-spin) until all of them have
    exited, and documents (issue #48's "any downtime documented") the moment any one of them
    exits non-zero while another is still running. `should_stop` (checked every heartbeat) ends
    the run the same way SIGINT/SIGTERM does. Returns exit_code 0 only if every child
    exited zero -- a child that never started at all (e.g. a missing binary) surfaces as an
    exception from spawning it, not a silent success."""
    heartbeat_log.parent.mkdir(parents=True, exist_ok=True)
    log_files: dict[str, object] = {}
    procs: dict[str, subprocess.Popen] = {}
    try:
        for spec in specs:
            spec.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = spec.log_path.open("ab")
            log_files[spec.name] = log_file
            procs[spec.name] = subprocess.Popen(  # noqa: S603 - fixed local commands built above
                spec.cmd,
                cwd=spec.cwd,
                env=spec.env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )

        stop_requested = False

        def _stop(_sig: int, _frame: object) -> None:
            nonlocal stop_requested
            stop_requested = True

        old_handlers = {sig: signal.signal(sig, _stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        downtime: list[dict[str, object]] = []
        reported_exit: set[str] = set()
        try:
            while True:
                statuses = {name: proc.poll() for name, proc in procs.items()}
                _write_line(
                    heartbeat_log,
                    {
                        "ts": dt.datetime.now(dt.UTC).isoformat(),
                        "event": "heartbeat",
                        "alive": {name: code is None for name, code in statuses.items()},
                        "exit_codes": {
                            name: code for name, code in statuses.items() if code is not None
                        },
                    },
                )
                for event in on_heartbeat() if on_heartbeat else []:
                    downtime.append(event)
                    _write_line(heartbeat_log, event)
                still_running = [name for name, code in statuses.items() if code is None]
                for name, code in statuses.items():
                    if code is not None and name not in reported_exit:
                        reported_exit.add(name)
                        if code != 0 and still_running:
                            event = {
                                "ts": dt.datetime.now(dt.UTC).isoformat(),
                                "event": "unexpected_exit",
                                "name": name,
                                "exit_code": code,
                                "still_running": list(still_running),
                            }
                            downtime.append(event)
                            _write_line(heartbeat_log, event)
                if not still_running:
                    break
                if stop_requested or (should_stop is not None and should_stop()):
                    for proc in procs.values():
                        if proc.poll() is None:
                            proc.terminate()
                    deadline = time.monotonic() + STOP_TIMEOUT_SEC
                    for proc in procs.values():
                        remaining = max(0.0, deadline - time.monotonic())
                        try:
                            proc.wait(timeout=remaining)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                    break
                time.sleep(poll_interval_sec)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
    finally:
        for f in log_files.values():
            f.close()  # type: ignore[attr-defined]

    exit_codes = [p.poll() for p in procs.values()]
    exit_code = 0 if exit_codes and all(c == 0 for c in exit_codes) else 1
    return RunResult(exit_code=exit_code, downtime=downtime)


def build_specs(
    args: argparse.Namespace,
    paths: ShadowPaths,
    mode: str,
    bridge_bin: Path,
    instrument: InstrumentConfig,
) -> list[ChildSpec]:
    paths.recording.parent.mkdir(parents=True, exist_ok=True)
    paths.out_dir.mkdir(parents=True, exist_ok=True)

    recorder_cmd = [
        str(AGENT_DIR / "node_modules" / ".bin" / "tsx"),
        "src/recorder/cli.ts",
        *(["--dry-run"] if args.dry_run else []),
        "--instrument-id",
        str(instrument.id),
        "--symbol",
        args.symbol,
        "--asset-class",
        args.asset_class,
        "--poll-interval-ms",
        str(args.poll_interval_ms),
        "--request-budget",
        str(args.request_budget),
        "--out",
        str(paths.recording),
        "--dataset",
        f"shadow-{paths.date}",
        "--catalog-dir",
        str(paths.catalog_dir),
    ]

    bridge_args = [
        str(paths.recording),
        "--venue",
        "sim",
        "--instrument",
        str(instrument.path),
        "--follow",
    ]
    if args.decide_every is not None:
        bridge_args += ["--decide-every", str(args.decide_every)]
    if args.stop_after_idle_ms is not None:
        bridge_args += ["--stop-after-idle-ms", str(args.stop_after_idle_ms)]

    agent_env = dict(os.environ)
    agent_env["QC_AGENT_MODE"] = mode
    agent_env["QC_INSTRUMENT"] = instrument.symbol
    agent_env["QC_BRIDGE_BIN"] = str(bridge_bin)
    agent_env["QC_BRIDGE_ARGS"] = " ".join(bridge_args)
    agent_env["QC_AGENT_LEDGER_PATH"] = str(paths.ledger)
    agent_env["QC_BRIDGE_DECISION_TIMEOUT_MS"] = "0"  # --follow: see module docstring
    agent_cmd = [str(AGENT_DIR / "node_modules" / ".bin" / "tsx"), "src/cli.ts"]

    return [
        ChildSpec(
            "recorder", recorder_cmd, AGENT_DIR, dict(os.environ), paths.out_dir / "recorder.log"
        ),
        ChildSpec("agent", agent_cmd, AGENT_DIR, agent_env, paths.out_dir / "agent.log"),
    ]


def write_reports(paths: ShadowPaths, date: str, root: Path = ROOT) -> bool:
    """Calls the existing scripts/reports/{ledger,pnl}_report.py against this run's own ledger
    (never fund/track-record/daily/ -- see module docstring). Read-only reuse: this script
    computes nothing about P&L or baselines itself."""
    paths.report_dir.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, argv in (
        (
            "ledger",
            [
                sys.executable,
                str(root / "scripts/reports/ledger_report.py"),
                "--ledger",
                str(paths.ledger),
                "--out",
                str(paths.report_dir / "ledger"),
            ],
        ),
        (
            "pnl",
            [
                sys.executable,
                str(root / "scripts/reports/pnl_report.py"),
                "--ledger",
                str(paths.ledger),
                "--date",
                date,
                "--out",
                str(paths.report_dir / "pnl"),
            ],
        ),
    ):
        result = subprocess.run(  # noqa: S603 - fixed local report scripts, argv built above
            argv, cwd=root, capture_output=True, text=True, check=False
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print(f"{name}_report.py exited {result.returncode}", file=sys.stderr)
            if name != "pnl":  # pnl_report.py's nonzero means "stop rule tripped" -- a finding.
                ok = False
    return ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--date", default=None, help="default: today, UTC, YYYY-MM-DD")
    p.add_argument(
        "--decider",
        choices=sorted(DECIDERS),
        default=None,
        help="fake (default); live = Claude, gated by ai_gate.py loop-agent-eval; openrouter = "
        "ADR-0042, gated by loop-agent-openrouter. A denied gate refuses, never runs fake.",
    )
    p.add_argument("--live", action="store_true", help="same as --decider live")
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--asset-class", default="equity", choices=["equity", "crypto"])
    p.add_argument("--poll-interval-ms", type=int, default=None)
    p.add_argument("--request-budget", type=int, default=None)
    p.add_argument("--decide-every", type=int, default=None)
    p.add_argument("--stop-after-idle-ms", type=int, default=None)
    p.add_argument(
        "--instrument-config",
        type=Path,
        default=None,
        help="instrument TOML for qc-bridge --instrument and the stalled-recording alert's "
        "trading hours (default: config/instruments/<symbol>.toml)",
    )
    p.add_argument("--stall-after-sec", type=float, default=alerts.DEFAULT_STALL_AFTER_SEC)
    args = p.parse_args(argv)

    if args.live:
        if args.decider not in (None, "live"):
            p.error(f"--live conflicts with --decider {args.decider}")
        args.decider = "live"
    args.decider = args.decider or "fake"
    if args.poll_interval_ms is None:
        args.poll_interval_ms = 100 if args.dry_run else 1000
    if args.request_budget is None:
        if args.dry_run:
            args.request_budget = 20
        else:
            p.error(
                "--request-budget must be sized explicitly for a real run (e.g. one poll/sec "
                "across a 6.5h session is ~23400; see agent/src/recorder/cli.ts's own note) -- "
                "there is no safe default: check your broker's rate limit, and a real run needs "
                "your broker adapter (QC_BROKER_MODULE)."
            )
    if args.decide_every is None:
        args.decide_every = 3 if args.dry_run else REAL_RUN_DECIDE_EVERY
    if args.stop_after_idle_ms is None and args.dry_run:
        args.stop_after_idle_ms = 400
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    date = args.date or today_utc()
    paths = paths_for(date)

    mode, loop = DECIDERS[args.decider]
    if loop is not None:
        allowed, reason = check_live_gate(loop=loop)
        if not allowed:
            print(f"refusing --decider {args.decider}: {reason}", file=sys.stderr)
            return 1
        print(f"ai-gate allowed --decider {args.decider}: {reason}")
    try:
        instrument = load_instrument(args.symbol, args.instrument_config)
    except (ValueError, KeyError, tomllib.TOMLDecodeError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 1

    try:
        bridge_bin = build_bridge_binary()
    except RuntimeError as e:
        print(f"cargo build -p qc-bridge failed: {e}", file=sys.stderr)
        return 1

    # Read the instrument calendar once before any child starts: a bad file must fail here, not
    # raise mid-run from the heartbeat loop and orphan the children.
    alerts.market_open(instrument.path, time.time_ns())
    specs = build_specs(args, paths, mode, bridge_bin, instrument)
    print(f"shadow {date}: mode={mode} recording={paths.recording} ledger={paths.ledger}")
    stall_watch = recording_stall_watch(paths.recording, instrument.path, args.stall_after_sec)
    result = run_children(specs, paths.heartbeat_log, on_heartbeat=stall_watch)
    if result.downtime:
        print(f"downtime documented: {len(result.downtime)} event(s) -- see {paths.heartbeat_log}")

    reports_ok = write_reports(paths, date)
    print(
        f"exit_code={result.exit_code} "
        f"downtime_events={len(result.downtime)} "
        f"reports_ok={reports_ok}"
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
