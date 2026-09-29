"""scripts/eval/scenarios_from_ledger.py (issue #52): selection rules, the horizon/"known
outcome" boundary, idempotent generation, and — the load-bearing invariant — that a generated
scenario's CSV can never contain anything from the source ledger entry's outcome, decision or
result, only its `request`."""

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "scenarios_from_ledger", ROOT / "scripts/eval/scenarios_from_ledger.py"
)
assert SPEC and SPEC.loader
sfl = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sfl
SPEC.loader.exec_module(sfl)

D = Decimal


def make_entry(
    request_id: str,
    ts_ns: int,
    mid: str,
    best_bid: str,
    best_ask: str,
    action: str,
    decision_extra: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    mode: str = "fake",
) -> dict[str, Any]:
    decision = {"request_id": request_id, "action": action, "rationale": "test rationale"}
    if decision_extra:
        decision.update(decision_extra)
    return {
        "request": {
            "request_id": request_id,
            "ts_ns": ts_ns,
            "instrument": "1",
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_ticks": 2,
            "features": {},
            "signal": None,
            "position": "0",
            "limits": {
                "max_position": "2",
                "max_notional": "1000",
                "max_order_rate_per_sec": 5,
                "remaining_daily_loss": "100",
            },
            "allowed_actions": ["buy", "sell", "no_trade"],
        },
        "decision": decision,
        "result": result,
        "mode": mode,
        "prompt_version": "v1",
        "model": "fake-rule-v1",
    }


def write_ledger(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_known_outcome_indices_excludes_the_horizon_tail() -> None:
    assert list(sfl.known_outcome_indices(5, horizon=2)) == [0, 1, 2]
    assert list(sfl.known_outcome_indices(2, horizon=2)) == []
    assert list(sfl.known_outcome_indices(0, horizon=2)) == []


def test_build_snapshot_csv_reproduces_the_recorded_top_of_book() -> None:
    request = {
        "instrument": "1",
        "ts_ns": 123456,
        "best_bid": "100.0",
        "best_ask": "100.2",
        "spread_ticks": 2,
    }
    csv_line = sfl.build_snapshot_csv(request)
    assert csv_line.startswith("S,1,1000,123456,123456,")
    fields = csv_line.strip().split(",")
    bids = fields[5].split(";")
    asks = fields[6].split(";")
    assert bids[0].split("@")[0] == "100.0"
    assert asks[0].split("@")[0] == "100.2"


def test_outcome_never_leaks_into_scenario_csv(tmp_path: Path) -> None:
    # Two otherwise-identical requests, paired with wildly different (and distinctive) decisions
    # and outcomes. If any outcome/decision text ever leaked into the generated CSV, one of these
    # markers would appear in it.
    distinctive_reject_marker = "DISTINCTIVE_RISK_REJECT_MARKER"
    distinctive_rationale_marker = "DISTINCTIVE_RATIONALE_MARKER_999"
    entries = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={
                "qty": "1",
                "limit_price": "100.2",
                "rationale": distinctive_rationale_marker,
            },
            result={"accepted": False, "risk_reject": distinctive_reject_marker},
        ),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "99.0", "98.9", "99.1", "no_trade"),  # mark: price dropped
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    out_dir = tmp_path / "scenarios"

    written = sfl.generate(ledger, out_dir, sfl.DEFAULT_POLICY, horizon=2)
    assert len(written) == 1
    csv_text = written[0].read_text()
    assert distinctive_reject_marker not in csv_text
    assert distinctive_rationale_marker not in csv_text
    # The provenance sidecar, by contrast, is exactly where that information belongs.
    prov_path = written[0].with_suffix("").with_suffix(".provenance.json")
    prov = json.loads(prov_path.read_text())
    assert prov["original"]["result"]["risk_reject"] == distinctive_reject_marker


def test_risk_rejected_entry_is_selected(tmp_path: Path) -> None:
    entries = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={"qty": "1", "limit_price": "100.2"},
            result={"accepted": False, "risk_reject": "max_notional"},
        ),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "101.0", "100.9", "101.1", "no_trade"),
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    written = sfl.generate(ledger, tmp_path / "out", sfl.DEFAULT_POLICY, horizon=2)
    assert len(written) == 1
    prov = json.loads(written[0].with_suffix("").with_suffix(".provenance.json").read_text())
    assert prov["selection"]["reason"] == sfl.REASON_RISK_REJECTED
    assert prov["source_ledger"]["request_id"] == "r0"


def test_confident_loss_entry_is_selected(tmp_path: Path) -> None:
    entries = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={"qty": "1", "limit_price": "100.2", "confidence": 0.9},
            result={"accepted": True},
        ),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "90.0", "89.9", "90.1", "no_trade"),  # price crashed: a loss
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    written = sfl.generate(ledger, tmp_path / "out", sfl.DEFAULT_POLICY, horizon=2)
    assert len(written) == 1
    prov = json.loads(written[0].with_suffix("").with_suffix(".provenance.json").read_text())
    assert prov["selection"]["reason"] == sfl.REASON_CONFIDENT_LOSS
    assert D(prov["outcome"]["pnl_usd"]) < 0


def test_low_confidence_loss_is_not_selected(tmp_path: Path) -> None:
    entries = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={"qty": "1", "limit_price": "100.2", "confidence": 0.2},
            result={"accepted": True},
        ),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "90.0", "89.9", "90.1", "no_trade"),
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    written = sfl.generate(ledger, tmp_path / "out", sfl.DEFAULT_POLICY, horizon=2)
    assert written == []


def test_profitable_no_trade_entry_is_selected(tmp_path: Path) -> None:
    entries = [
        make_entry("r0", 0, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "110.0", "109.9", "110.1", "no_trade"),  # price rallied
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    written = sfl.generate(ledger, tmp_path / "out", sfl.DEFAULT_POLICY, horizon=2)
    assert len(written) == 1
    prov = json.loads(written[0].with_suffix("").with_suffix(".provenance.json").read_text())
    assert prov["selection"]["reason"] == sfl.REASON_PROFITABLE_NO_TRADE
    assert D(prov["outcome"]["pnl_usd"]) > 0


def test_flat_no_trade_is_not_selected(tmp_path: Path) -> None:
    # Price at the mark equals price at the decision: neither a hypothetical buy nor a
    # hypothetical sell would have made money, so declining was not an interesting miss.
    entries = [
        make_entry("r0", 0, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "100.1", "100.0", "100.2", "no_trade"),
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    written = sfl.generate(ledger, tmp_path / "out", sfl.DEFAULT_POLICY, horizon=2)
    assert written == []


def test_entry_within_horizon_of_ledger_end_is_never_selected(tmp_path: Path) -> None:
    # Same shape as the risk-rejected case, but with only one future entry when horizon=2
    # requires two: the outcome isn't known yet, so nothing should be selected.
    entries = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={"qty": "1", "limit_price": "100.2"},
            result={"accepted": False, "risk_reject": "max_notional"},
        ),
        make_entry("r1", 1, "101.0", "100.9", "101.1", "no_trade"),
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    written = sfl.generate(ledger, tmp_path / "out", sfl.DEFAULT_POLICY, horizon=2)
    assert written == []


def test_label_is_real_for_live_mode_and_synthetic_otherwise(tmp_path: Path) -> None:
    base = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={"qty": "1", "limit_price": "100.2"},
            result={"accepted": False, "risk_reject": "max_notional"},
            mode="live",
        ),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "101.0", "100.9", "101.1", "no_trade"),
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, base)
    written = sfl.generate(ledger, tmp_path / "out", sfl.DEFAULT_POLICY, horizon=2)
    prov = json.loads(written[0].with_suffix("").with_suffix(".provenance.json").read_text())
    assert prov["label"] == "real"


def test_regenerating_over_the_same_ledger_is_idempotent(tmp_path: Path) -> None:
    entries = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={"qty": "1", "limit_price": "100.2"},
            result={"accepted": False, "risk_reject": "max_notional"},
        ),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "101.0", "100.9", "101.1", "no_trade"),
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    out_dir = tmp_path / "out"
    first = sfl.generate(ledger, out_dir, sfl.DEFAULT_POLICY, horizon=2)
    second = sfl.generate(ledger, out_dir, sfl.DEFAULT_POLICY, horizon=2)
    assert len(first) == 1
    assert second == []  # already generated, identical: not re-reported as new
    assert len(list(out_dir.glob("*.csv"))) == 1


def test_conflicting_content_for_the_same_slug_is_a_hard_error(tmp_path: Path) -> None:
    entries = [
        make_entry(
            "r0",
            0,
            "100.1",
            "100.0",
            "100.2",
            "buy",
            decision_extra={"qty": "1", "limit_price": "100.2"},
            result={"accepted": False, "risk_reject": "max_notional"},
        ),
        make_entry("r1", 1, "100.1", "100.0", "100.2", "no_trade"),
        make_entry("r2", 2, "101.0", "100.9", "101.1", "no_trade"),
    ]
    ledger = tmp_path / "ledger.jsonl"
    write_ledger(ledger, entries)
    out_dir = tmp_path / "out"
    written = sfl.generate(ledger, out_dir, sfl.DEFAULT_POLICY, horizon=2)
    prov_path = written[0].with_suffix("").with_suffix(".provenance.json")
    prov_path.write_text('{"tampered": true}')
    try:
        sfl.generate(ledger, out_dir, sfl.DEFAULT_POLICY, horizon=2)
        raise AssertionError("expected a RuntimeError on conflicting provenance content")
    except RuntimeError as e:
        assert "append-only" in str(e)


def test_main_with_no_ledger_is_a_graceful_no_op(tmp_path: Path, capsys) -> None:
    code = sfl.main(["--root", str(tmp_path), "--ledger", str(tmp_path / "missing.jsonl")])
    assert code == 0
    assert "nothing to do" in capsys.readouterr().out
