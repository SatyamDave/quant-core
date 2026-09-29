#!/usr/bin/env python3
"""Alert checker for the live host (#64). Standard library only.

Reads qc-bridge's JSON-lines log, the agent's spend ledger, and (issue #45/#70) the daily P&L
report `scripts/reports/pnl_report.py` writes to `fund/track-record/daily/<date>.json`, raising an
alert for each of:
  - halt              any {"event": "halt"} line (any reason)
  - kill_switch       a halt with reason "kill_switch", or a future "kill_switch_engaged" event
  - reconciliation_mismatch   a halt with reason "reconciliation"
  - repeated_risk_rejects     >= --reject-threshold {"event": "risk_reject"} lines within a
                               trailing --reject-window-sec window
  - spend_cap         the agent ledger's cost_usd sum for the current UTC day exceeds
                       --spend-ratio of --daily-cap-usd
  - stop_rule         the daily P&L report at --pnl-report has `stop_rule.triggered: true`
  - recording_stalled the price recording at --recording (qc-bridge --follow's input) has not
                       been written for more than --stall-after-sec while --instrument's market is
                       open; a missing file during open hours also fires (fail closed)

Integration notes (for the bridge-v1.1 lane; this is the *contract* this checker needs from
qc-bridge's own log, not something read from a merged bridge-v1.1 PR -- it runs in parallel):

qc-bridge has no structured event log today (`engine/crates/bridge/src/main.rs` only
`eprintln!`s on a fatal startup error). Its `submit_order_intent` and `status` wire results
already carry a small snake_case vocabulary though (`risk_reject_code()` and the `halted` field
in `engine/crates/bridge/src/engine.rs`): `"kill_switch"`, `"max_daily_loss"` today for halts,
and `"kill_switch"`, `"invalid_order"`, `"stale_data"`, `"max_daily_loss"`, `"price_band"`,
`"max_notional"`, `"max_position"`, `"max_order_rate"` for a rejected order. This checker's log
contract reuses that exact vocabulary (one set of strings, not a second one invented for
logging) plus two forward-compatible reasons protocol v1.1's `report_execution` is expected to
introduce for external-venue reconciliation: `"reconciliation"` and `"illegal_order_event"`.
One JSON object per line, written to the path ops/live/supervisor.py redirects the bridge
child's stdout to (<log-dir>/qc-bridge.log):
    {"ts_ns": <u64>, "event": "halt", "reason": <one of the halt reasons above>,
     "detail"?: <string>}
    {"ts_ns": <u64>, "event": "risk_reject", "reason": <one of the reject reasons above>,
     "instrument"?: <string>, "detail"?: <string>}
    {"ts_ns": <u64>, "event": "kill_switch_engaged", "source": <string>}  # optional, for #44
`ts_ns` matches the convention already used on the wire (`DecisionRequest.ts_ns` in
`agent/src/types.ts`).

Agent decision ledger: real today at out/agent/ledger.jsonl (gitignored; agent/src/ledger.ts),
one `DecisionLedgerEntry` JSON object per line (`agent/src/types.ts`). This checker reads two
fields: the UTC-day bucket from the nested `request.ts_ns` (matching `DecisionRequest.ts_ns`;
there is no top-level `ts_ns` on a ledger entry) and the top-level, optional `cost_usd`. `cost_usd`
is a decimal string (`agent/src/types.ts`, wave 2 -- it was a JSON number through wave 1); summing
it here via `Decimal(str(cost_usd))` works the same either way (a pre-wave-2 ledger line with a
bare float still parses), so this checker needed no code change for that fix, only this note.

Daily P&L report (issue #70, `scripts/reports/pnl_report.py`, `fund/track-record/README.md`):
one JSON object, not JSON-lines, at `--pnl-report` (default: none -- this check is skipped
unless given a path). This checker reads exactly one field, `stop_rule.triggered`; the report's
own thresholds (placeholders, per that script) are not re-evaluated
here. Fires once per calendar date recorded in the report's own `date` field (dedup key
`stop_rule:<date>`), not once per run, so a human isn't re-alerted every 1-2 minutes for the same
day's already-seen trip.

Recording stall (#48): qc-bridge --follow waits indefinitely for the recording to grow, so a
recorder that silently stops (dead sign-in, network, crash) looks exactly like a quiet market.
"Grown" is the file's mtime: the recorder only ever appends, so a stale mtime means no new rows.
Market hours come from the instrument TOML's `[trading_hours]` and its calendar (holidays, early
closes), the same files qc-bridge's `MarketCalendar` reads. A date outside the calendar's
coverage years is treated as a regular session, so the check errs toward alerting.
scripts/shadow/run.py runs this check on every heartbeat for data/raw/shadow/<date>.csv; on the
live host pass the recording qc-bridge follows (the first positional in QC_BRIDGE_ARGS).

Usage (run periodically, e.g. from a systemd timer every 1-2 minutes -- "within minutes" per
#64 does not need a persistent tailing process). --bridge-log points at whichever log file
actually carries qc-bridge's output: ops/live/logs/agent.log in today's one-process topology
(it already carries qc-bridge's forwarded stderr), or ops/live/logs/qc-bridge.log if
supervisor.py's --bridge-cmd is used (see ops/live/README.md):
    alerts.py --bridge-log ops/live/logs/agent.log --agent-ledger out/agent/ledger.jsonl \\
        --daily-cap-usd 5.00 --state-file ops/live/state/alerts-seen.json
scripts/ops/daily_close.py (issue #45/#70) calls this checker with --pnl-report set, once a day,
after writing that day's broker-sourced record.
Exit code: 1 if any alert fired this run, 0 otherwise (so a systemd OnFailure= hook, or a cron
mailer, can double as a second notification path).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

SECOND_NS = 1_000_000_000
DAY_NS = 86_400 * SECOND_NS

# Placeholder AI-model call budget (separate from the trading dollar caps) -- deliberately
# small and conservative, override with --daily-cap-usd or QC_DAILY_SPEND_CAP_USD.
DEFAULT_DAILY_CAP_USD = Decimal("5.00")

DEFAULT_INSTRUMENT = Path(__file__).resolve().parents[2] / "config/instruments/spy.toml"
# The recorder polls once a second in a real run; two minutes of silence is well past a slow poll.
DEFAULT_STALL_AFTER_SEC = 120.0
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

RUNBOOKS = {
    "halt": "docs/runbooks/kill-switch.md",
    "kill_switch": "docs/runbooks/kill-switch.md",
    "reconciliation_mismatch": "docs/runbooks/reconciliation-mismatch.md",
    "repeated_risk_rejects": "docs/runbooks/repeated-risk-rejects.md",
    "spend_cap": "docs/runbooks/spend-cap.md",
    "stop_rule": "docs/runbooks/stop-rule-tripped.md",
    "take_profit_reached": "docs/runbooks/stop-rule-tripped.md",
    "recording_stalled": "docs/runbooks/recording-stalled.md",
}


@dataclass(frozen=True)
class Alert:
    rule: str
    severity: str
    message: str
    dedup_key: str | None  # None means "always resend" (a rate/level condition, not an event)

    @property
    def runbook(self) -> str:
        return RUNBOOKS[self.rule]


class Notifier:
    def send(self, alert: Alert) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    """Default: dry-run, prints only. No network call."""

    def __init__(self, stream: object = None) -> None:
        self.stream = stream or sys.stdout

    def send(self, alert: Alert) -> None:
        print(
            f"[ALERT] {alert.severity.upper()} {alert.rule}: {alert.message} "
            f"(runbook: {alert.runbook})",
            file=self.stream,  # type: ignore[arg-type]
        )


class WebhookNotifier(Notifier):
    """POSTs {"rule","severity","message","runbook"} as JSON. Works with any JSON-webhook
    receiver (an ntfy.sh topic URL, a Slack incoming webhook, a custom endpoint) -- no real
    endpoint is configured anywhere in this repo; pass --webhook-url to opt in. Email is
    documented as an alternative in ops/live/README.md rather than implemented here, since it
    needs real SMTP credentials this build has none of.
    """

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, alert: Alert) -> None:
        body = json.dumps(
            {
                "rule": alert.rule,
                "severity": alert.severity,
                "message": alert.message,
                "runbook": alert.runbook,
            }
        ).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - operator-configured webhook URL
            self.url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            resp.read()


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a malformed line must not take the whole checker down
    return events


def check_halts(events: list[dict[str, object]]) -> list[Alert]:
    alerts = []
    for e in events:
        if e.get("event") != "halt":
            continue
        ts_ns = e.get("ts_ns")
        reason = e.get("reason", "unknown")
        alerts.append(Alert("halt", "critical", f"engine halted: reason={reason}", f"halt:{ts_ns}"))
        if reason == "kill_switch":
            alerts.append(
                Alert("kill_switch", "critical", "kill switch engaged", f"kill_switch:{ts_ns}")
            )
        elif reason == "reconciliation":
            alerts.append(
                Alert(
                    "reconciliation_mismatch",
                    "critical",
                    "reconciliation mismatch caused a halt",
                    f"reconciliation_mismatch:{ts_ns}",
                )
            )
    return alerts


def check_kill_switch_engaged(events: list[dict[str, object]]) -> list[Alert]:
    """A future, more immediate signal than waiting for the halt line (#44's external trigger)."""
    return [
        Alert(
            "kill_switch",
            "critical",
            f"kill switch engaged (source={e.get('source', 'unknown')})",
            f"kill_switch:{e.get('ts_ns')}",
        )
        for e in events
        if e.get("event") == "kill_switch_engaged"
    ]


def check_repeated_risk_rejects(
    events: list[dict[str, object]], now_ns: int, window_sec: float, threshold: int
) -> list[Alert]:
    window_ns = int(window_sec * SECOND_NS)
    recent = [
        e
        for e in events
        if e.get("event") == "risk_reject"
        and isinstance(e.get("ts_ns"), int)
        and now_ns - e["ts_ns"] <= window_ns  # type: ignore[operator]
    ]
    if len(recent) < threshold:
        return []
    reasons = sorted({str(e.get("reason", "unknown")) for e in recent})
    return [
        Alert(
            "repeated_risk_rejects",
            "warning",
            f"{len(recent)} risk rejects in the last {window_sec:.0f}s "
            f"(threshold {threshold}); reasons: {', '.join(reasons)}",
            dedup_key=None,  # a rate condition: keep alerting while it holds
        )
    ]


def _ledger_entry_ts_ns(entry: dict[str, object]) -> int | None:
    """A DecisionLedgerEntry has no top-level ts_ns; it's nested at request.ts_ns
    (agent/src/types.ts). Accept a top-level ts_ns too, for a simpler log shape."""
    if isinstance(entry.get("ts_ns"), int):
        return entry["ts_ns"]  # type: ignore[return-value]
    request = entry.get("request")
    if isinstance(request, dict) and isinstance(request.get("ts_ns"), int):
        return request["ts_ns"]  # type: ignore[return-value]
    return None


def check_spend_cap(
    ledger: list[dict[str, object]], now_ns: int, daily_cap_usd: Decimal, ratio: float
) -> list[Alert]:
    day_start_ns = (now_ns // DAY_NS) * DAY_NS
    total = Decimal("0")
    for e in ledger:
        ts_ns = _ledger_entry_ts_ns(e)
        if ts_ns is None or ts_ns < day_start_ns or ts_ns > now_ns:
            continue
        try:
            total += Decimal(str(e.get("cost_usd", 0)))
        except InvalidOperation:
            continue  # a malformed cost_usd must not take the whole checker down
    threshold = daily_cap_usd * Decimal(str(ratio))
    if total <= threshold:
        return []
    return [
        Alert(
            "spend_cap",
            "warning",
            f"today's AI spend ${total} exceeds {ratio:.0%} of the ${daily_cap_usd} daily cap",
            dedup_key=None,  # a level condition: keep alerting while it holds
        )
    ]


def check_stop_rule(pnl_report: dict[str, object] | None) -> list[Alert]:
    """`pnl_report` is the parsed daily record `scripts/reports/pnl_report.py` writes (issue #70)
    -- `None` (no `--pnl-report` given, or the file doesn't exist yet) means "not checked," which
    must never be silently treated as "clean." A malformed report fails loud (KeyError/TypeError
    propagates) rather than being swallowed like a malformed *log line* is elsewhere in this
    file -- a whole missing/broken report is an operational problem serious enough to want a
    traceback, not a skip."""
    if pnl_report is None:
        return []
    stop_rule = pnl_report.get("stop_rule")
    if not isinstance(stop_rule, dict) or not stop_rule.get("triggered"):
        return []
    date = pnl_report.get("date", "unknown-date")
    reasons = "; ".join(str(r) for r in stop_rule.get("reasons", [])) or "no reason recorded"
    return [
        Alert(
            "stop_rule", "critical", f"stop rule tripped for {date}: {reasons}", f"stop_rule:{date}"
        )
    ]


def check_take_profit_reached(pnl_report: dict[str, object] | None) -> list[Alert]:
    """Take-profit rule: a distinct, named alert (separate from the generic `stop_rule`
    one above, which also fires for this) so a human sees at a glance that trading stopped because
    the account made its target, not because something went wrong. Same "None/malformed fails
    loud" contract as `check_stop_rule`."""
    if pnl_report is None:
        return []
    stop_rule = pnl_report.get("stop_rule")
    if not isinstance(stop_rule, dict) or not stop_rule.get("take_profit_reached"):
        return []
    date = pnl_report.get("date", "unknown-date")
    return [
        Alert(
            "take_profit_reached",
            "critical",
            f"take-profit target reached for {date}: trading halted for good",
            f"take_profit_reached:{date}",
        )
    ]


def market_open(instrument_path: Path, now_ns: int) -> tuple[bool, str]:
    """(open?, note). Reads the instrument's `[trading_hours]` and the calendar it names
    (resolved relative to the instrument file, like `main.rs::load_instrument`)."""
    with instrument_path.open("rb") as f:
        hours = tomllib.load(f)["trading_hours"]
    with (instrument_path.parent / hours["calendar"]).open("rb") as f:
        calendar = tomllib.load(f)
    local = dt.datetime.fromtimestamp(now_ns / SECOND_NS, ZoneInfo(hours["tz"]))
    day = local.date().isoformat()
    if _WEEKDAYS[local.weekday()] not in hours["days"]:
        return False, ""
    holidays = {h["date"] for h in calendar.get("holidays", [])}
    early = {e["date"]: e["close"] for e in calendar.get("early_closes", [])}
    if day in holidays:
        return False, ""
    covered = {d[:4] for d in holidays | early.keys()}
    note = "" if day[:4] in covered else f" (calendar has no {day[:4]} entries; assumed open)"
    now_hm = local.strftime("%H:%M")
    return hours["open"] <= now_hm < early.get(day, hours["close"]), note


def check_recording_stalled(
    recording: Path, instrument_path: Path, now_ns: int, stall_after_sec: float
) -> list[Alert]:
    is_open, note = market_open(instrument_path, now_ns)
    if not is_open:
        return []
    if not recording.exists():
        message = f"recording {recording} does not exist during market hours{note}"
    else:
        idle_sec = (now_ns - recording.stat().st_mtime_ns) / SECOND_NS
        if idle_sec <= stall_after_sec:
            return []
        message = (
            f"recording {recording} has not grown for {idle_sec:.0f}s "
            f"(limit {stall_after_sec:.0f}s) during market hours{note}"
        )
    return [Alert("recording_stalled", "critical", message, dedup_key=None)]


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("seen", []))
    except (ValueError, OSError):
        return set()


def save_state(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"seen": sorted(seen)}, indent=0), encoding="utf-8")


def dedup(alerts: list[Alert], seen: set[str]) -> list[Alert]:
    fresh = []
    for a in alerts:
        if a.dedup_key is None or a.dedup_key not in seen:
            fresh.append(a)
        if a.dedup_key is not None:
            seen.add(a.dedup_key)
    return fresh


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bridge-log", type=Path, action="append", default=[], dest="bridge_logs")
    p.add_argument("--agent-ledger", type=Path)
    p.add_argument("--state-file", type=Path, default=Path("ops/live/state/alerts-seen.json"))
    p.add_argument("--reject-window-sec", type=float, default=60.0)
    p.add_argument("--reject-threshold", type=int, default=3)
    p.add_argument("--daily-cap-usd", type=Decimal, default=DEFAULT_DAILY_CAP_USD)
    p.add_argument("--spend-ratio", type=float, default=0.8)
    p.add_argument("--webhook-url", help="POST alerts here instead of printing (no default)")
    p.add_argument("--now-ns", type=int, help="override 'now' for deterministic testing")
    p.add_argument(
        "--pnl-report",
        type=Path,
        help="scripts/reports/pnl_report.py's daily record (fund/track-record/daily/<date>.json); "
        "alerts if its stop_rule.triggered is true (issue #70). Omit to skip this check.",
    )
    p.add_argument(
        "--recording",
        type=Path,
        help="the price recording qc-bridge --follow reads; alerts if it stops growing during "
        "--instrument's market hours. Omit to skip this check.",
    )
    p.add_argument("--instrument", type=Path, default=DEFAULT_INSTRUMENT)
    p.add_argument("--stall-after-sec", type=float, default=DEFAULT_STALL_AFTER_SEC)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now_ns = args.now_ns if args.now_ns is not None else time.time_ns()
    notifier: Notifier = (
        WebhookNotifier(args.webhook_url) if args.webhook_url else ConsoleNotifier()
    )

    alerts: list[Alert] = []
    for log_path in args.bridge_logs:
        events = read_jsonl(log_path)
        alerts += check_halts(events)
        alerts += check_kill_switch_engaged(events)
        alerts += check_repeated_risk_rejects(
            events, now_ns, args.reject_window_sec, args.reject_threshold
        )
    if args.agent_ledger:
        ledger = read_jsonl(args.agent_ledger)
        alerts += check_spend_cap(ledger, now_ns, args.daily_cap_usd, args.spend_ratio)
    if args.pnl_report and args.pnl_report.exists():
        alerts += check_stop_rule(json.loads(args.pnl_report.read_text(encoding="utf-8")))
    if args.recording:
        alerts += check_recording_stalled(
            args.recording, args.instrument, now_ns, args.stall_after_sec
        )

    seen = load_state(args.state_file)
    fresh = dedup(alerts, seen)
    for alert in fresh:
        notifier.send(alert)
    save_state(args.state_file, seen)
    return 1 if fresh else 0


if __name__ == "__main__":
    sys.exit(main())
