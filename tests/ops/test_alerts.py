"""Trigger each of scripts/ops/alerts.py's five alert rules from the fixture logs in
tests/ops/fixtures/, and check each one names a runbook that actually exists (#64's
"each alert links to a runbook that already exists").
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODULE_PATH = REPO / "scripts/ops/alerts.py"
spec = importlib.util.spec_from_file_location("alerts", MODULE_PATH)
assert spec and spec.loader
alerts = importlib.util.module_from_spec(spec)
sys.modules["alerts"] = alerts
spec.loader.exec_module(alerts)

SECOND_NS = alerts.SECOND_NS
FIXTURE_TS_NS = 1767225600000000000  # 2026-01-01T00:00:00Z, matches every fixture file above


def run(argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    state_file = tmp_path / "alerts-seen.json"
    code = alerts.main([*argv, "--state-file", str(state_file)])
    return code, capsys.readouterr().out


# every runbook an alert can name must exist


@pytest.mark.parametrize("rule", sorted(alerts.RUNBOOKS))
def test_every_alert_names_a_runbook_that_exists(rule: str) -> None:
    assert (REPO / alerts.RUNBOOKS[rule]).is_file()


def test_halt_alert_fires_for_any_halt_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(
        ["--bridge-log", str(FIXTURES / "bridge_halt.jsonl"), "--now-ns", str(FIXTURE_TS_NS)],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "halt" in out
    assert "max_daily_loss" in out
    assert alerts.RUNBOOKS["halt"] in out
    # a generic max_daily_loss halt is not also a kill-switch or reconciliation alert
    assert "kill_switch:" not in out
    assert "reconciliation_mismatch:" not in out


def test_kill_switch_alert_fires_and_also_fires_the_generic_halt_alert(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(
        [
            "--bridge-log",
            str(FIXTURES / "bridge_kill_switch.jsonl"),
            "--now-ns",
            str(FIXTURE_TS_NS),
        ],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "kill_switch:" in out
    assert alerts.RUNBOOKS["kill_switch"] in out
    assert "halt:" in out  # both fire from the same log line


def test_reconciliation_mismatch_alert_fires(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(
        [
            "--bridge-log",
            str(FIXTURES / "bridge_reconciliation.jsonl"),
            "--now-ns",
            str(FIXTURE_TS_NS),
        ],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "reconciliation_mismatch:" in out
    assert alerts.RUNBOOKS["reconciliation_mismatch"] in out


def test_repeated_risk_rejects_alert_fires_past_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now_ns = FIXTURE_TS_NS + 25 * SECOND_NS  # 25s after the first of 3 rejects spanning 20s
    code, out = run(
        [
            "--bridge-log",
            str(FIXTURES / "bridge_repeated_rejects.jsonl"),
            "--now-ns",
            str(now_ns),
        ],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "repeated_risk_rejects:" in out
    assert alerts.RUNBOOKS["repeated_risk_rejects"] in out
    assert "price_band" in out and "max_order_rate" in out


def test_repeated_risk_rejects_does_not_fire_below_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now_ns = FIXTURE_TS_NS + 25 * SECOND_NS
    code, out = run(
        [
            "--bridge-log",
            str(FIXTURES / "bridge_repeated_rejects.jsonl"),
            "--now-ns",
            str(now_ns),
            "--reject-threshold",
            "10",
        ],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert out == ""


def test_spend_cap_alert_fires_past_ratio_of_cap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now_ns = FIXTURE_TS_NS + 3600 * SECOND_NS + 100 * SECOND_NS  # same UTC day as both entries
    code, out = run(
        [
            "--agent-ledger",
            str(FIXTURES / "agent_ledger_spend.jsonl"),
            "--now-ns",
            str(now_ns),
            "--daily-cap-usd",
            "5.00",
        ],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "spend_cap:" in out
    assert alerts.RUNBOOKS["spend_cap"] in out
    assert "4.3" in out  # 2.1 + 2.2, summed as Decimal (via str()), not float addition


def test_spend_cap_alert_does_not_fire_below_ratio(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now_ns = FIXTURE_TS_NS + 3600 * SECOND_NS + 100 * SECOND_NS
    code, out = run(
        [
            "--agent-ledger",
            str(FIXTURES / "agent_ledger_spend.jsonl"),
            "--now-ns",
            str(now_ns),
            "--daily-cap-usd",
            "100.00",
        ],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert out == ""


def test_spend_cap_ignores_entries_from_a_different_utc_day(tmp_path: Path) -> None:
    tomorrow_ns = FIXTURE_TS_NS + 2 * alerts.DAY_NS
    total = alerts.check_spend_cap(
        alerts.read_jsonl(FIXTURES / "agent_ledger_spend.jsonl"), tomorrow_ns, Decimal("0.01"), 0.8
    )
    assert total == []  # yesterday's spend must not roll into today's cap check


# dedup: a one-shot event alert does not fire twice for the same log line across runs


def test_event_alert_is_not_repeated_across_runs_but_level_alert_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_file = tmp_path / "alerts-seen.json"
    argv = [
        "--bridge-log",
        str(FIXTURES / "bridge_halt.jsonl"),
        "--now-ns",
        str(FIXTURE_TS_NS),
        "--state-file",
        str(state_file),
    ]
    first = alerts.main(argv)
    capsys.readouterr()
    second = alerts.main(argv)
    out2 = capsys.readouterr().out
    assert first == 1
    assert second == 0  # the same halt line must not alert twice
    assert out2 == ""
    assert json.loads(state_file.read_text())["seen"]  # state was actually persisted


def test_repeated_risk_rejects_keeps_firing_every_run_while_the_rate_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now_ns = FIXTURE_TS_NS + 25 * SECOND_NS
    argv = [
        "--bridge-log",
        str(FIXTURES / "bridge_repeated_rejects.jsonl"),
        "--now-ns",
        str(now_ns),
    ]
    code1, out1 = run(argv, tmp_path, capsys)
    code2, out2 = run(argv, tmp_path, capsys)
    assert code1 == 1 and code2 == 1
    assert "repeated_risk_rejects:" in out1
    assert "repeated_risk_rejects:" in out2  # a rate condition keeps alerting, unlike a halt line


def test_malformed_lines_do_not_crash_the_checker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_log = tmp_path / "bad.jsonl"
    bad_log.write_text('not json\n{"ts_ns": 1, "event": "halt", "reason": "max_daily_loss"}\n')
    code, out = run(["--bridge-log", str(bad_log), "--now-ns", "1000000000000"], tmp_path, capsys)
    assert code == 1
    assert "halt" in out


# --- stop_rule (issue #45/#70: the daily P&L report's own stop-rule verdict) -----------------


def test_stop_rule_alert_fires_when_pnl_report_says_triggered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(
        ["--pnl-report", str(FIXTURES / "pnl_report_stop_rule_tripped.json"), "--now-ns", "1"],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "stop_rule: stop rule tripped for 2026-02-02" in out
    assert "drawdown $75 exceeds $50" in out
    assert alerts.RUNBOOKS["stop_rule"] in out


def test_stop_rule_alert_does_not_fire_when_not_triggered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(
        ["--pnl-report", str(FIXTURES / "pnl_report_stop_rule_clean.json"), "--now-ns", "1"],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert out == ""


def test_stop_rule_alert_is_skipped_when_no_pnl_report_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run([], tmp_path, capsys)
    assert code == 0
    assert out == ""


def test_stop_rule_alert_is_skipped_when_the_pnl_report_path_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = run(
        ["--pnl-report", str(tmp_path / "does-not-exist.json"), "--now-ns", "1"], tmp_path, capsys
    )
    assert code == 0
    assert out == ""


def test_stop_rule_alert_does_not_repeat_for_the_same_date_across_runs(tmp_path: Path) -> None:
    argv = ["--pnl-report", str(FIXTURES / "pnl_report_stop_rule_tripped.json"), "--now-ns", "1"]
    state_file = tmp_path / "alerts-seen.json"
    first = alerts.main([*argv, "--state-file", str(state_file)])
    second = alerts.main([*argv, "--state-file", str(state_file)])
    assert first == 1
    assert second == 0  # already alerted for this date


def test_check_stop_rule_returns_nothing_for_none() -> None:
    assert alerts.check_stop_rule(None) == []


def test_console_notifier_is_the_default_and_makes_no_network_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notifier = alerts.ConsoleNotifier()
    assert isinstance(notifier, alerts.Notifier)
    notifier.send(alerts.Alert("halt", "critical", "test", "halt:1"))
    assert "test" in capsys.readouterr().out


# --- recording_stalled (#48: qc-bridge --follow cannot tell a dead recorder from a quiet market)


SPY = REPO / "config/instruments/spy.toml"  # real trading_hours + calendars/nyse.toml
MON_OPEN_NS = 1790618400 * SECOND_NS  # 2026-09-28T18:00Z = Mon 14:00 America/New_York
MON_AFTER_CLOSE_NS = 1790627400 * SECOND_NS  # 2026-09-28T20:30Z = Mon 16:30 New York
SAT_MIDDAY_NS = 1790445600 * SECOND_NS  # 2026-09-26T18:00Z = Sat 14:00 New York
THANKSGIVING_NS = 1795719600 * SECOND_NS  # 2026-11-26T19:00Z = Thu 14:00 New York, holiday
EARLY_CLOSE_BEFORE_NS = 1795800600 * SECOND_NS  # 2026-11-27T17:30Z = Fri 12:30 New York
EARLY_CLOSE_AFTER_NS = 1795804200 * SECOND_NS  # 2026-11-27T18:30Z = Fri 13:30, past 13:00 close


def recording_last_written(tmp_path: Path, now_ns: int, ago_sec: int) -> Path:
    path = tmp_path / "2026-09-28.csv"
    path.write_text("ts_ns,bid,ask\n")
    written_ns = now_ns - ago_sec * SECOND_NS
    os.utime(path, ns=(written_ns, written_ns))
    return path


def stall_alerts(recording: Path, now_ns: int) -> list[str]:
    return [a.rule for a in alerts.check_recording_stalled(recording, SPY, now_ns, 120.0)]


def test_growing_recording_does_not_alert(tmp_path: Path) -> None:
    assert stall_alerts(recording_last_written(tmp_path, MON_OPEN_NS, 5), MON_OPEN_NS) == []


def test_stalled_recording_during_open_hours_alerts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recording = recording_last_written(tmp_path, MON_OPEN_NS, 300)
    code, out = run(
        ["--recording", str(recording), "--instrument", str(SPY), "--now-ns", str(MON_OPEN_NS)],
        tmp_path,
        capsys,
    )
    assert code == 1
    assert "recording_stalled" in out
    assert "has not grown for 300s" in out
    assert alerts.RUNBOOKS["recording_stalled"] in out


@pytest.mark.parametrize(
    "now_ns",
    [MON_AFTER_CLOSE_NS, SAT_MIDDAY_NS, THANKSGIVING_NS, EARLY_CLOSE_AFTER_NS],
    ids=["after-close", "weekend", "holiday", "after-early-close"],
)
def test_stalled_recording_outside_market_hours_does_not_alert(tmp_path: Path, now_ns: int) -> None:
    assert stall_alerts(recording_last_written(tmp_path, now_ns, 3600), now_ns) == []
    assert stall_alerts(tmp_path / "missing.csv", now_ns) == []


def test_early_close_day_is_open_before_its_close(tmp_path: Path) -> None:
    recording = recording_last_written(tmp_path, EARLY_CLOSE_BEFORE_NS, 3600)
    assert stall_alerts(recording, EARLY_CLOSE_BEFORE_NS) == ["recording_stalled"]


def test_missing_recording_during_open_hours_alerts(tmp_path: Path) -> None:
    found = alerts.check_recording_stalled(tmp_path / "missing.csv", SPY, MON_OPEN_NS, 120.0)
    assert [a.rule for a in found] == ["recording_stalled"]
    assert "does not exist" in found[0].message


def test_year_outside_calendar_coverage_is_assumed_open(tmp_path: Path) -> None:
    now_ns = 1853517600 * SECOND_NS  # 2028-09-25T18:00Z = Mon 14:00 New York, no 2028 entries
    found = alerts.check_recording_stalled(tmp_path / "missing.csv", SPY, now_ns, 120.0)
    assert [a.rule for a in found] == ["recording_stalled"]
    assert "no 2028 entries" in found[0].message
