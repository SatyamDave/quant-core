#!/usr/bin/env python3
"""Fail-closed supervisor for the live trading host (#63). Standard library only.

Today's real topology (agent/src/bridge.ts's BridgeClient): the agent service is the one
top-level process; it spawns qc-bridge itself as its own child over a stdio pipe (QC_BRIDGE_BIN
/ QC_BRIDGE_ARGS) and already fails when that child exits (BridgeClient.failEverything()
rejects every pending call, which propagates out of runLoop() and exits the agent process).
So --agent-cmd alone already gets "a crash of either halts both" for free from the agent's own
code. --bridge-cmd is optional and mainly for a future topology where qc-bridge runs as its own
long-lived process instead (e.g. once protocol v1.1's external-venue mode makes it a persistent
daemon rather than a one-shot subprocess) -- when given, both are supervised as independent
siblings and a crash of either brings both down together, matching issue #63's "starts qc-bridge
+ agent service."

Fail-closed preflight, run before the first start and before every restart:
  1. the halt marker (--halt-marker) must not already exist -- refuses to start, does not
     clear it. Clearing means a human has read why it halted (mirrors qc_risk::KillSwitch and
     autonomy/PAUSE: no automatic reset).
  2. every name in --require-env must be a non-empty environment variable (secrets: the
     broker adapter, ANTHROPIC_API_KEY -- read from the environment only, never from a
     file this script parses; see ops/live/bin/load-secrets.sh for how they get there).
  3. if --health-check is given, that command must exit 0 (a pluggable dependency check --
     e.g. "can qc-bridge reach the configured venue port").
Any failure writes the halt marker with a reason and exits 1 without starting a child.

Restart-with-backoff: on any child's exit, every other supervised child is terminated (SIGTERM,
then SIGKILL after --stop-timeout-sec), backoff sleeps (exponential from --backoff-base-sec,
capped at --backoff-max-sec, doubling once per crash and resetting after --stable-after-sec of
clean running), preflight runs again, and only then does everything restart together. A crash
rate past --max-restarts within --restart-window-sec writes the halt marker with reason
"crash-loop" and stops retrying -- a tight restart loop is not "healthy," it's a symptom.

Every child's stdout/stderr is redirected to <log-dir>/<name>.log (append mode; the agent's log
already carries qc-bridge's forwarded stderr too, per BridgeClient's `child.stderr.pipe(...)`)
-- this is the file scripts/ops/alerts.py reads, and what an off-host log shipper (see
ops/live/log-shipping/) tails. The supervisor's own lifecycle events go to
<log-dir>/supervisor.log as JSON lines: {"ts_ns", "event": "child_exited"|"restart"|"halt",
"name"?, "exit_code"?, "reason"?}.

Usage (today's real topology -- the agent spawns its own qc-bridge child):
  QC_BRIDGE_BIN=/opt/quant-core/engine/target/release/qc-bridge \\
  QC_BRIDGE_ARGS="--venue sim" \\
  supervisor.py --agent-cmd 'npx --prefix agent tsx src/cli.ts' \\
      --require-env ANTHROPIC_API_KEY

Usage (a future standalone qc-bridge daemon, supervised as its own sibling process):
  supervisor.py --bridge-cmd 'qc-bridge --venue sim' \\
      --agent-cmd 'npx --prefix agent tsx src/cli.ts' \\
      --require-env ANTHROPIC_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HALT_MARKER = Path("ops/live/state/HALT")
DEFAULT_LOG_DIR = Path("ops/live/logs")


def now_ns() -> int:
    return time.time_ns()


@dataclass
class Config:
    agent_cmd: str
    # Optional: today the agent spawns its own qc-bridge child (see module docstring). Set this
    # only to supervise a standalone qc-bridge process as an independent sibling.
    bridge_cmd: str | None = None
    require_env: list[str] = field(default_factory=list)
    health_check: str | None = None
    halt_marker: Path = DEFAULT_HALT_MARKER
    log_dir: Path = DEFAULT_LOG_DIR
    max_restarts: int = 5
    restart_window_sec: float = 300.0
    backoff_base_sec: float = 1.0
    backoff_max_sec: float = 60.0
    stable_after_sec: float = 60.0
    stop_timeout_sec: float = 5.0
    poll_interval_sec: float = 0.2


class Halted(Exception):
    """Raised internally when preflight refuses to (re)start; carries the reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def write_event(log_dir: Path, event: dict[str, object]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts_ns": now_ns(), **event}, sort_keys=True)
    with (log_dir / "supervisor.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def write_halt_marker(path: Path, reason: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts_ns": now_ns(), "reason": reason}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def preflight(cfg: Config) -> None:
    """Raises Halted (and writes the marker) on any failure. Never clears an existing marker."""
    if cfg.halt_marker.exists():
        raise Halted(f"halt marker present: {cfg.halt_marker}")
    for name in cfg.require_env:
        if not os.environ.get(name):
            reason = f"missing secret: {name} is not set in the environment"
            write_halt_marker(cfg.halt_marker, reason)
            raise Halted(reason)
    if cfg.health_check:
        result = subprocess.run(  # noqa: S603 - operator-configured health check command
            shlex.split(cfg.health_check), capture_output=True, check=False, timeout=30
        )
        if result.returncode != 0:
            reason = f"unhealthy dependency: {cfg.health_check!r} exited {result.returncode}"
            write_halt_marker(cfg.halt_marker, reason)
            raise Halted(reason)


def start_children(cfg: Config) -> dict[str, subprocess.Popen[bytes]]:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    children: dict[str, subprocess.Popen[bytes]] = {}
    wanted = [("agent", cfg.agent_cmd)]
    if cfg.bridge_cmd:  # optional: today the agent spawns its own qc-bridge child
        wanted.append(("qc-bridge", cfg.bridge_cmd))
    for name, cmd in wanted:
        log_path = cfg.log_dir / f"{name}.log"
        log_file = log_path.open("ab")
        children[name] = subprocess.Popen(  # noqa: S603 - operator-configured commands
            shlex.split(cmd), stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
        )
    return children


def stop_children(children: dict[str, subprocess.Popen[bytes]], stop_timeout_sec: float) -> None:
    for proc in children.values():
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + stop_timeout_sec
    for proc in children.values():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=stop_timeout_sec)


class Supervisor:
    """The restart-with-backoff loop, as a class so tests can drive it one cycle at a time."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.restart_times: deque[float] = deque()
        self.consecutive_failures = 0
        self.last_start_monotonic = 0.0
        self.stopped = False

    def _backoff_sec(self) -> float:
        return min(
            self.cfg.backoff_base_sec * (2**self.consecutive_failures), self.cfg.backoff_max_sec
        )

    def start(self) -> None:
        preflight(self.cfg)
        self.children = start_children(self.cfg)
        self.last_start_monotonic = time.monotonic()

    def crashed_child(self) -> tuple[str, int] | None:
        """Returns (name, exit_code) for the first child that has exited, if any."""
        for name, proc in self.children.items():
            code = proc.poll()
            if code is not None:
                return name, code
        return None

    def handle_crash(self, name: str, exit_code: int) -> None:
        """One restart cycle: stop every other supervised child, backoff, preflight, restart all.

        Raises Halted if the halt marker gets written (preflight failure or crash-loop) --
        callers should stop the outer loop on that, per issue #63's "fail closed."
        """
        write_event(
            self.cfg.log_dir, {"event": "child_exited", "name": name, "exit_code": exit_code}
        )
        stop_children(self.children, self.cfg.stop_timeout_sec)

        if time.monotonic() - self.last_start_monotonic >= self.cfg.stable_after_sec:
            self.consecutive_failures = 0
        now = time.monotonic()
        self.restart_times.append(now)
        while self.restart_times and now - self.restart_times[0] > self.cfg.restart_window_sec:
            self.restart_times.popleft()
        if len(self.restart_times) > self.cfg.max_restarts:
            reason = (
                f"crash-loop: {len(self.restart_times)} restarts in "
                f"{self.cfg.restart_window_sec:.0f}s (limit {self.cfg.max_restarts})"
            )
            write_halt_marker(self.cfg.halt_marker, reason)
            write_event(self.cfg.log_dir, {"event": "halt", "reason": reason})
            raise Halted(reason)

        delay = self._backoff_sec()
        self.consecutive_failures += 1
        write_event(self.cfg.log_dir, {"event": "restart", "backoff_sec": delay})
        time.sleep(delay)
        self.start()  # re-runs preflight; raises Halted if a secret vanished meanwhile

    def run_forever(self) -> int:
        try:
            self.start()
        except Halted as h:
            write_event(self.cfg.log_dir, {"event": "halt", "reason": h.reason})
            return 1

        def _stop(_sig: int, _frame: object) -> None:
            self.stopped = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        while not self.stopped:
            crash = self.crashed_child()
            if crash is None:
                time.sleep(self.cfg.poll_interval_sec)
                continue
            try:
                self.handle_crash(*crash)
            except Halted as h:
                write_event(self.cfg.log_dir, {"event": "halt", "reason": h.reason})
                return 1
        stop_children(self.children, self.cfg.stop_timeout_sec)
        return 0


def parse_args(argv: list[str] | None) -> Config:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--bridge-cmd",
        help="optional: a standalone qc-bridge command line, supervised as its own sibling "
        "process. Omit it when the agent spawns its own qc-bridge child (today's topology; "
        "set QC_BRIDGE_BIN/QC_BRIDGE_ARGS for --agent-cmd instead).",
    )
    p.add_argument("--agent-cmd", required=True, help="agent service command line")
    p.add_argument(
        "--require-env", action="append", default=[], help="env var that must be set (repeatable)"
    )
    p.add_argument("--health-check", help="command that must exit 0 before (re)starting")
    p.add_argument("--halt-marker", type=Path, default=DEFAULT_HALT_MARKER)
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    p.add_argument("--max-restarts", type=int, default=5)
    p.add_argument("--restart-window-sec", type=float, default=300.0)
    p.add_argument("--backoff-base-sec", type=float, default=1.0)
    p.add_argument("--backoff-max-sec", type=float, default=60.0)
    p.add_argument("--stable-after-sec", type=float, default=60.0)
    p.add_argument("--stop-timeout-sec", type=float, default=5.0)
    a = p.parse_args(argv)
    return Config(
        bridge_cmd=a.bridge_cmd,
        agent_cmd=a.agent_cmd,
        require_env=a.require_env,
        health_check=a.health_check,
        halt_marker=a.halt_marker,
        log_dir=a.log_dir,
        max_restarts=a.max_restarts,
        restart_window_sec=a.restart_window_sec,
        backoff_base_sec=a.backoff_base_sec,
        backoff_max_sec=a.backoff_max_sec,
        stable_after_sec=a.stable_after_sec,
        stop_timeout_sec=a.stop_timeout_sec,
    )


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    return Supervisor(cfg).run_forever()


if __name__ == "__main__":
    sys.exit(main())
