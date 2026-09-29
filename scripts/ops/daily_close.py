#!/usr/bin/env python3
"""Issue #45 (scheduling) + #70: the once-a-day close for the live/canary host.

  scripts/ops/daily_close.py [--date YYYY-MM-DD] [--bridge-log PATH] [--ledger PATH]
      [--broker-journal PATH] [--policy PATH] [--out DIR] [--agent-ledger PATH]
      [--state-file PATH] [--webhook-url URL] [--reconcile-cmd CMD] [--now-ns N]

## What "runs reconcile" means here

`agent/src/loop.ts`'s `runLoop()` already calls `reconcile()` once at startup (before any
decision) and again periodically, whenever the agent runs in external venue mode
(`agent/src/broker/external-mode.ts`) -- wave 2's "at startup, periodically, and after any
ambiguous response". The continuously-supervised production process (`ops/live/supervisor.py`)
already gets that for free every time it starts or restarts.

This script's own job is the once-a-day CHECKPOINT issue #45 asks for ("each day's check adds a
new record"): `--reconcile-cmd`, if given, is any operator-supplied command that performs one such
reconcile-triggering pass (in production: whatever restarts or checkpoints the supervised agent
service so its startup `reconcile()` actually runs against the day's broker state; in tests, a
fixture double). Its exit code is checked, but never *trusted alone* -- this script independently
scans the bridge's own structured log (`scripts/ops/alerts.py`'s log contract) for a
`"reconciliation"`-reason halt dated today, the same event `check_halts()` already turns into a
`reconciliation_mismatch` alert. Neither side is silently trusted over the other (root CLAUDE.md
rule 10): no `--reconcile-cmd` and no matching log line records `"unknown"`, never `"ok"` by
default. `ops/live/README.md`'s own status note is candid that no live host or venue adapter
exists yet, so `"unknown"` is the honest, expected result until one does; this script is written
for that day, not pretending it has already arrived.

## What it does after that

Writes the broker-sourced daily P&L record (`scripts/reports/pnl_report.py --source broker`) with
that day's reconciliation verdict embedded, then runs the same checks
`scripts/ops/alerts.py` runs on a live host (halts, kill switch, repeated risk rejects, spend cap)
plus the stop-rule check, against the just-written record -- so a reconcile mismatch or a
stop-rule trip alerts exactly the way any other live-host condition already does. Unlike a
drawdown or days-behind trip, `stop_rule.take_profit_reached` (the "stop trading for good at the
take-profit target" rule) also writes `--halt-marker`
(`ops/live/supervisor.py`'s own file and format): the account made its target, so nothing should
restart it automatically, only a human clearing the marker by hand.

Exit code: nonzero if `--reconcile-cmd` failed, the day's reconciliation verdict is not `"ok"`,
the stop rule tripped, or any other alert fired. 0 only when every one of those is clean -- so a
systemd timer's own `OnFailure=` can gate the same way `scripts/ops/alerts.py`'s does.

Standard library only, matching every other script in scripts/ops and scripts/reports.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shlex
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/reports"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "ops/live"))
import alerts  # noqa: E402
import pnl_report  # noqa: E402
import supervisor  # noqa: E402 - reuses write_halt_marker: one halt-marker writer, not two

DEFAULT_BRIDGE_LOG = ROOT / "ops/live/logs/agent.log"
DEFAULT_STATE_FILE = ROOT / "ops/live/state/alerts-seen.json"
DEFAULT_HALT_MARKER = ROOT / "ops/live/state/HALT"


def _day(ts_ns: int) -> str:
    return dt.datetime.fromtimestamp(ts_ns / 1e9, tz=dt.UTC).date().isoformat()


def reconciliation_status_for_date(bridge_log: Path, date: str) -> tuple[str, str | None]:
    """Scans one bridge log for a reconciliation-mismatch halt dated `date` (UTC, from the
    event's own `ts_ns` -- the bridge's market clock, matching the wire convention everywhere
    else on this protocol). A found line always means "mismatch"; NOT finding one only means
    "unknown" -- the bridge might not have run today, or this might not be the right log file --
    never "ok". Only `--reconcile-cmd` actually succeeding can produce "ok" (see `close_day`)."""
    for event in alerts.read_jsonl(bridge_log):
        if event.get("event") != "halt" or event.get("reason") != "reconciliation":
            continue
        ts_ns = event.get("ts_ns")
        if isinstance(ts_ns, int) and _day(ts_ns) == date:
            return "mismatch", str(event.get("detail", "reconciliation mismatch"))
    return "unknown", None


def run_reconcile_cmd(cmd: str) -> tuple[bool, str]:
    """Runs an operator-supplied command that performs one reconcile-triggering pass (module
    docstring). A failing exit code, a timeout, or the command failing to start at all is a hard
    "not ok" -- never assumed clean."""
    try:
        # Split into argv and run without a shell: the command comes from config, but a shell
        # would turn any stray metacharacter in it into code.
        argv = shlex.split(cmd)
        proc = subprocess.run(  # noqa: S603 - argv from operator config, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=300,  # supervisor.py's --health-check
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return False, f"--reconcile-cmd failed to run: {err}"
    if proc.returncode != 0:
        return False, f"--reconcile-cmd exited {proc.returncode}: {proc.stderr.strip()[:500]}"
    return True, ""


def determine_reconciliation(
    bridge_log: Path, date: str, reconcile_cmd: str | None
) -> dict[str, object]:
    cmd_ok: bool | None = None
    cmd_detail = ""
    if reconcile_cmd is not None:
        cmd_ok, cmd_detail = run_reconcile_cmd(reconcile_cmd)

    log_status, log_detail = reconciliation_status_for_date(bridge_log, date)
    if log_status == "mismatch":
        # The bridge's own record of a mismatch outranks a reconcile-cmd that merely didn't
        # error -- the command can exit 0 without the bridge itself having found the books clean.
        return {"status": "mismatch", "detail": log_detail}
    if cmd_ok is False:
        return {"status": "mismatch", "detail": cmd_detail}
    if cmd_ok is True:
        return {"status": "ok", "detail": None}
    return {
        "status": "unknown",
        "detail": "no --reconcile-cmd given and no reconciliation event found in the bridge log",
    }


def close_day(args: argparse.Namespace) -> int:
    date = args.date or dt.datetime.now(dt.UTC).date().isoformat()
    reconciliation = determine_reconciliation(args.bridge_log, date, args.reconcile_cmd)

    result, _curve = pnl_report.build_report(
        args.ledger,
        args.policy,
        source=pnl_report.SOURCE_RECONCILED,
        broker_journal=args.broker_journal,
    )
    result.reconciliation = reconciliation
    path = pnl_report.write_daily_record(result, date, args.out)
    print(pnl_report.render_markdown(result, date))
    print(f"(written to {path})")

    now_ns = args.now_ns if args.now_ns is not None else time.time_ns()
    bridge_events = alerts.read_jsonl(args.bridge_log)
    alert_list: list[alerts.Alert] = []
    alert_list += alerts.check_halts(bridge_events)
    alert_list += alerts.check_kill_switch_engaged(bridge_events)
    alert_list += alerts.check_repeated_risk_rejects(
        bridge_events, now_ns, args.reject_window_sec, args.reject_threshold
    )
    if args.agent_ledger:
        ledger_entries = alerts.read_jsonl(args.agent_ledger)
        alert_list += alerts.check_spend_cap(
            ledger_entries, now_ns, args.daily_cap_usd, args.spend_ratio
        )
    written_record = json.loads(path.read_text(encoding="utf-8"))
    alert_list += alerts.check_stop_rule(written_record)
    alert_list += alerts.check_take_profit_reached(written_record)
    if result.stop_rule.get("take_profit_reached"):
        # Permanent, not a retry-after-backoff halt like a crash-loop: the account hit its target,
        # so no new session may start until a human clears this by hand (same contract
        # ops/live/supervisor.py's own preflight() and scripts/ops/live_canary.py's start refusal
        # already give every other halt-marker reason).
        supervisor.write_halt_marker(
            args.halt_marker, f"take-profit target reached: {result.stop_rule['reasons'][-1]}"
        )

    notifier: alerts.Notifier = (
        alerts.WebhookNotifier(args.webhook_url) if args.webhook_url else alerts.ConsoleNotifier()
    )
    seen = alerts.load_state(args.state_file)
    fresh = alerts.dedup(alert_list, seen)
    for alert in fresh:
        notifier.send(alert)
    alerts.save_state(args.state_file, seen)

    if reconciliation["status"] != "ok":
        print(
            f"reconciliation: {reconciliation['status']} ({reconciliation.get('detail')})",
            file=sys.stderr,
        )

    clean = reconciliation["status"] == "ok" and not result.stop_rule["triggered"] and not fresh
    return 0 if clean else 1


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", type=str, default=None)
    p.add_argument("--bridge-log", type=Path, default=DEFAULT_BRIDGE_LOG)
    p.add_argument("--ledger", type=Path, default=pnl_report.DEFAULT_LEDGER)
    p.add_argument("--broker-journal", type=Path, default=pnl_report.DEFAULT_BROKER_JOURNAL)
    p.add_argument("--policy", type=Path, default=pnl_report.DEFAULT_POLICY)
    p.add_argument("--out", type=Path, default=pnl_report.DEFAULT_OUT)
    p.add_argument(
        "--agent-ledger",
        type=Path,
        default=None,
        help="defaults to --ledger's value (the same decision ledger the spend-cap check reads)",
    )
    p.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    p.add_argument(
        "--halt-marker",
        type=Path,
        default=DEFAULT_HALT_MARKER,
        help="written (ops/live/supervisor.py's format) when the take-profit rule reaches its "
        "target, so the supervisor and `just live-canary` both refuse to start a new session",
    )
    p.add_argument("--webhook-url", help="POST alerts here instead of printing (no default)")
    p.add_argument(
        "--reconcile-cmd",
        help="a command that performs one reconcile-triggering pass before the close (module "
        "docstring); omit to rely only on scanning --bridge-log for today's outcome",
    )
    p.add_argument("--reject-window-sec", type=float, default=60.0)
    p.add_argument("--reject-threshold", type=int, default=3)
    p.add_argument("--daily-cap-usd", type=Decimal, default=alerts.DEFAULT_DAILY_CAP_USD)
    p.add_argument("--spend-ratio", type=float, default=0.8)
    p.add_argument("--now-ns", type=int, help="override 'now' for deterministic testing")
    args = p.parse_args(argv)
    if args.agent_ledger is None:
        args.agent_ledger = args.ledger
    return args


def main(argv: list[str] | None = None) -> int:
    return close_day(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
