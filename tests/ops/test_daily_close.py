"""scripts/ops/daily_close.py: the once-a-day close (issue #45 scheduling + #70). Fixture-driven,
same convention as tests/ops/test_alerts.py and tests/ops/test_supervisor.py -- no live host or
venue adapter exists yet (ops/live/README.md), so --reconcile-cmd is always a fixture double here,
never the real agent binary.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts/reports"))
sys.path.insert(0, str(REPO / "scripts/ops"))


OK_CMD = f'{sys.executable} -c "import sys; sys.exit(0)"'
FAIL_CMD = f'{sys.executable} -c "import sys; sys.exit(1)"'


def _load(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pnl_report = _load("pnl_report", "scripts/reports/pnl_report.py")
alerts = _load("alerts", "scripts/ops/alerts.py")
daily_close = _load("daily_close", "scripts/ops/daily_close.py")

ONE_DAY_NS = 86_400_000_000_000
DATE0 = "2026-02-02"
TS0 = 1769990400000000000  # 2026-02-02T00:00:00Z


def _approval_payload(client_order_id: int) -> str:
    return json.dumps(
        {"client_order_id": client_order_id, "request_id": "dr-1", "side": "buy"},
        separators=(",", ":"),
    )


def _ledger_entry(client_order_id: int, qty: str, decision_price: str) -> str:
    return json.dumps(
        {
            "request": {
                "request_id": "dr-1",
                "ts_ns": TS0,
                "instrument": "1",
                "best_bid": decision_price,
                "best_ask": decision_price,
                "mid": decision_price,
                "spread_ticks": 0,
                "features": {},
                "signal": None,
                "position": "0",
                "limits": {
                    "max_position": "10",
                    "max_notional": "10000",
                    "max_order_rate_per_sec": 5,
                    "remaining_daily_loss": "1000",
                },
                "allowed_actions": ["buy", "sell", "no_trade"],
            },
            "decision": {
                "request_id": "dr-1",
                "action": "buy",
                "qty": qty,
                "limit_price": decision_price,
                "rationale": "t",
            },
            "result": {
                "accepted": True,
                "client_order_id": client_order_id,
                "approval": {"payload": _approval_payload(client_order_id), "signature": "c3lu"},
            },
            "mode": "fake",
            "prompt_version": "v1",
            "model": "test",
            "cost_usd": "0.01",
        }
    )


def _no_trade_entry(ts_ns: int, mid: str) -> str:
    return json.dumps(
        {
            "request": {
                "request_id": "dr-0",
                "ts_ns": ts_ns,
                "instrument": "1",
                "best_bid": mid,
                "best_ask": mid,
                "mid": mid,
                "spread_ticks": 0,
                "features": {},
                "signal": None,
                "position": "0",
                "limits": {
                    "max_position": "10",
                    "max_notional": "10000",
                    "max_order_rate_per_sec": 5,
                    "remaining_daily_loss": "1000",
                },
                "allowed_actions": ["buy", "sell", "no_trade"],
            },
            "decision": {"request_id": "dr-0", "action": "no_trade", "rationale": "t"},
            "result": None,
            "mode": "fake",
            "prompt_version": "v1",
            "model": "test",
            "cost_usd": "0.0",
        }
    )


def _journal_resolved(client_order_id: int, status: str, **fields: object) -> str:
    return json.dumps(
        {
            "client_order_id": client_order_id,
            "ref_id": pnl_report.ref_id_for_approval(_approval_payload(client_order_id)),
            "phase": "resolved",
            "result": {"raw": {}, "status": status, **fields},
        }
    )


def _bridge_halt_reconciliation(ts_ns: int, detail: str = "unknown venue order 7") -> str:
    return json.dumps(
        {"ts_ns": ts_ns, "event": "halt", "reason": "reconciliation", "detail": detail}
    )


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Path]:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(_ledger_entry(1, "1", "100") + "\n")
    journal = tmp_path / "broker-journal.jsonl"
    journal.write_text(_journal_resolved(1, "filled", filled_qty="1", avg_price="100") + "\n")
    bridge_log = tmp_path / "agent.log"
    bridge_log.write_text("")
    return {
        "ledger": ledger,
        "journal": journal,
        "bridge_log": bridge_log,
        "out": tmp_path / "daily",
        "state_file": tmp_path / "alerts-seen.json",
    }


def _argv(ws: dict[str, Path], **extra: str) -> list[str]:
    argv = [
        "--date",
        DATE0,
        "--bridge-log",
        str(ws["bridge_log"]),
        "--ledger",
        str(ws["ledger"]),
        "--broker-journal",
        str(ws["journal"]),
        "--out",
        str(ws["out"]),
        "--state-file",
        str(ws["state_file"]),
        "--now-ns",
        str(TS0),
    ]
    for k, v in extra.items():
        argv += [f"--{k.replace('_', '-')}", v]
    return argv


# --- reconciliation_status_for_date / run_reconcile_cmd / determine_reconciliation -------------


def test_reconciliation_status_finds_a_same_day_mismatch(workspace: dict[str, Path]) -> None:
    workspace["bridge_log"].write_text(_bridge_halt_reconciliation(TS0) + "\n")
    status, detail = daily_close.reconciliation_status_for_date(workspace["bridge_log"], DATE0)
    assert status == "mismatch"
    assert "unknown venue order" in detail


def test_reconciliation_status_ignores_a_different_days_mismatch(
    workspace: dict[str, Path],
) -> None:
    other_day_ts = TS0 - ONE_DAY_NS
    workspace["bridge_log"].write_text(_bridge_halt_reconciliation(other_day_ts) + "\n")
    status, _detail = daily_close.reconciliation_status_for_date(workspace["bridge_log"], DATE0)
    assert status == "unknown"


def test_reconciliation_status_unknown_for_empty_log(workspace: dict[str, Path]) -> None:
    status, detail = daily_close.reconciliation_status_for_date(workspace["bridge_log"], DATE0)
    assert status == "unknown"
    assert detail is None


def test_run_reconcile_cmd_ok_on_zero_exit() -> None:
    ok, detail = daily_close.run_reconcile_cmd(f'{sys.executable} -c "import sys; sys.exit(0)"')
    assert ok is True
    assert detail == ""


def test_run_reconcile_cmd_not_ok_on_nonzero_exit() -> None:
    ok, detail = daily_close.run_reconcile_cmd(
        f"{sys.executable} -c \"import sys; sys.stderr.write('boom'); sys.exit(3)\""
    )
    assert ok is False
    assert "exited 3" in detail
    assert "boom" in detail


def test_determine_reconciliation_log_mismatch_outranks_a_successful_cmd(
    workspace: dict[str, Path],
) -> None:
    workspace["bridge_log"].write_text(_bridge_halt_reconciliation(TS0) + "\n")
    result = daily_close.determine_reconciliation(workspace["bridge_log"], DATE0, OK_CMD)
    assert result["status"] == "mismatch"


def test_determine_reconciliation_ok_when_cmd_succeeds_and_log_is_clean(
    workspace: dict[str, Path],
) -> None:
    result = daily_close.determine_reconciliation(workspace["bridge_log"], DATE0, OK_CMD)
    assert result == {"status": "ok", "detail": None}


def test_determine_reconciliation_mismatch_when_cmd_fails(workspace: dict[str, Path]) -> None:
    result = daily_close.determine_reconciliation(workspace["bridge_log"], DATE0, "exit 1")
    assert result["status"] == "mismatch"


def test_determine_reconciliation_unknown_when_no_cmd_and_clean_log(
    workspace: dict[str, Path],
) -> None:
    result = daily_close.determine_reconciliation(workspace["bridge_log"], DATE0, None)
    assert result["status"] == "unknown"


# --- main(): end-to-end close ------------------------------------------------------------------


def test_close_writes_broker_sourced_record_and_exits_clean(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    code = daily_close.main(_argv(workspace, reconcile_cmd=OK_CMD))
    assert code == 0
    written = json.loads((workspace["out"] / f"{DATE0}.json").read_text())
    assert written["source"] == "reconciled"
    assert written["reconciliation"] == {"status": "ok", "detail": None}
    assert written["fills"] == {"accepted_orders": 1, "matched_fills": 1}


def test_close_exits_nonzero_and_alerts_on_reconcile_mismatch(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    workspace["bridge_log"].write_text(_bridge_halt_reconciliation(TS0) + "\n")
    code = daily_close.main(_argv(workspace))
    out = capsys.readouterr().out
    assert code == 1
    written = json.loads((workspace["out"] / f"{DATE0}.json").read_text())
    assert written["reconciliation"]["status"] == "mismatch"
    assert "reconciliation_mismatch:" in out  # the same alert alerts.py's own check_halts fires


def test_close_exits_nonzero_when_reconcile_cmd_fails(workspace: dict[str, Path]) -> None:
    code = daily_close.main(_argv(workspace, reconcile_cmd=FAIL_CMD))
    assert code == 1
    written = json.loads((workspace["out"] / f"{DATE0}.json").read_text())
    assert written["reconciliation"]["status"] == "mismatch"


def test_close_exits_nonzero_on_stop_rule_trip(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    tight_policy = workspace["out"].parent / "tight_policy.toml"
    tight_policy.write_text(
        '[fees]\nfee_bps = "0"\n'
        '[stop_rule]\nmax_drawdown_usd = "0.01"\nmax_consecutive_days_behind_buy_and_hold = 999\n'
    )
    # Drawdown needs a peak-to-trough drop across at least two points: a flat no_trade entry
    # establishes an equity peak of 0, then the buy entry -- filled at 150 by the broker while the
    # mark stays 100 -- drops equity to -50.
    workspace["ledger"].write_text(
        _no_trade_entry(TS0, "100") + "\n" + _ledger_entry(1, "1", "100") + "\n"
    )
    workspace["journal"].write_text(
        _journal_resolved(1, "filled", filled_qty="1", avg_price="150") + "\n"
    )
    code = daily_close.main(_argv(workspace, reconcile_cmd=OK_CMD, policy=str(tight_policy)))
    out = capsys.readouterr().out
    assert code == 1
    assert "stop_rule:" in out


def _take_profit_policy(path: Path, take_profit_usd: str = "400") -> Path:
    # Isolates the take-profit check: drawdown and days-behind thresholds are set so loose they
    # cannot also trip, so a triggered result below is only ever the take-profit rule.
    path.write_text(
        '[fees]\nfee_bps = "0"\n'
        '[stop_rule]\nmax_drawdown_usd = "100000"\n'
        f'max_consecutive_days_behind_buy_and_hold = 999\ntake_profit_usd = "{take_profit_usd}"\n'
    )
    return path


def test_close_writes_halt_marker_and_alerts_when_take_profit_reached(
    workspace: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    policy = _take_profit_policy(workspace["out"].parent / "take_profit_policy.toml")
    # Bought 1@100 (broker-filled at 100, no fee), then marked at 500: total_pnl == 500-100 == 400.
    workspace["ledger"].write_text(
        _ledger_entry(1, "1", "100") + "\n" + _no_trade_entry(TS0 + ONE_DAY_NS, "500") + "\n"
    )
    workspace["journal"].write_text(
        _journal_resolved(1, "filled", filled_qty="1", avg_price="100") + "\n"
    )
    halt_marker = workspace["out"].parent / "HALT"
    code = daily_close.main(
        _argv(workspace, reconcile_cmd=OK_CMD, policy=str(policy), halt_marker=str(halt_marker))
    )
    out = capsys.readouterr().out
    written = json.loads((workspace["out"] / f"{DATE0}.json").read_text())
    assert written["agent"]["total_pnl_after_fees_usd"] == "400"
    assert written["stop_rule"]["take_profit_reached"] is True
    assert code == 1
    assert halt_marker.exists()
    assert "take_profit_reached:" in out
    assert json.loads(halt_marker.read_text())["reason"]  # supervisor.py's own halt-marker format


def test_close_does_not_write_halt_marker_below_take_profit_target(
    workspace: dict[str, Path],
) -> None:
    policy = _take_profit_policy(workspace["out"].parent / "take_profit_policy.toml")
    # Same shape, one cent short of the target: total_pnl == 499.99-100 == 399.99.
    workspace["ledger"].write_text(
        _ledger_entry(1, "1", "100") + "\n" + _no_trade_entry(TS0 + ONE_DAY_NS, "499.99") + "\n"
    )
    workspace["journal"].write_text(
        _journal_resolved(1, "filled", filled_qty="1", avg_price="100") + "\n"
    )
    halt_marker = workspace["out"].parent / "HALT"
    code = daily_close.main(
        _argv(workspace, reconcile_cmd=OK_CMD, policy=str(policy), halt_marker=str(halt_marker))
    )
    assert code == 0
    assert not halt_marker.exists()


def test_close_does_not_write_halt_marker_for_a_drawdown_only_trip(
    workspace: dict[str, Path],
) -> None:
    # The existing max_drawdown stop rule is unchanged: a drawdown-only trip still only alerts
    # and exits nonzero, it must not also start writing the permanent halt marker.
    tight_policy = workspace["out"].parent / "tight_policy.toml"
    tight_policy.write_text(
        '[fees]\nfee_bps = "0"\n'
        '[stop_rule]\nmax_drawdown_usd = "0.01"\nmax_consecutive_days_behind_buy_and_hold = 999\n'
    )
    workspace["ledger"].write_text(
        _no_trade_entry(TS0, "100") + "\n" + _ledger_entry(1, "1", "100") + "\n"
    )
    workspace["journal"].write_text(
        _journal_resolved(1, "filled", filled_qty="1", avg_price="150") + "\n"
    )
    halt_marker = workspace["out"].parent / "HALT"
    code = daily_close.main(
        _argv(
            workspace, reconcile_cmd=OK_CMD, policy=str(tight_policy), halt_marker=str(halt_marker)
        )
    )
    assert code == 1
    assert not halt_marker.exists()


def test_close_is_idempotent_for_an_unchanged_day(workspace: dict[str, Path]) -> None:
    argv = _argv(workspace, reconcile_cmd=OK_CMD)
    first = daily_close.main(argv)
    second = daily_close.main(argv)
    assert first == 0
    assert second == 0  # re-running the same day's close must not raise (append-only, same content)


def test_close_never_reports_ok_by_default_with_no_reconcile_cmd(
    workspace: dict[str, Path],
) -> None:
    code = daily_close.main(_argv(workspace))  # no --reconcile-cmd, clean log
    assert code == 1  # "unknown" is not "ok" -- fails closed, never silently trusted clean
    written = json.loads((workspace["out"] / f"{DATE0}.json").read_text())
    assert written["reconciliation"]["status"] == "unknown"


def test_run_reconcile_cmd_never_runs_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "injected"
    daily_close.run_reconcile_cmd(f"{OK_CMD} ; touch {marker}")
    assert not marker.exists(), "shell metacharacters must not be interpreted"
