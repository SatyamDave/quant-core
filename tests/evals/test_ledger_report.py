"""scripts/reports/ledger_report.py: deterministic rendering of a fixture ledger, hand-computed
expected values for every aggregate, and the two anomaly detectors (issue #39)."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "ledger_report", ROOT / "scripts/reports/ledger_report.py"
)
assert SPEC and SPEC.loader
ledger_report = importlib.util.module_from_spec(SPEC)
# dataclasses resolves annotations via sys.modules[cls.__module__]; register before exec_module.
sys.modules[SPEC.name] = ledger_report
SPEC.loader.exec_module(ledger_report)


def _request(request_id: str, ts_ns: int) -> dict:
    return {
        "request_id": request_id,
        "ts_ns": ts_ns,
        "instrument": "1",
        "best_bid": "100.0",
        "best_ask": "100.2",
        "mid": "100.1",
        "spread_ticks": 2,
        "features": {},
        "signal": None,
        "position": "0.0",
        "limits": {
            "max_position": "0.01",
            "max_notional": "500",
            "max_order_rate_per_sec": 5,
            "remaining_daily_loss": "100",
        },
        "allowed_actions": ["buy", "sell", "no_trade"],
    }


def _entry(
    request_id: str,
    ts_ns: int,
    action: str,
    result: dict | None,
    rationale: str = "ordinary decision",
    latency_ms: float | None = None,
    cost_usd: float | None = None,
    mode: str = "fake",
) -> dict:
    decision = {"request_id": request_id, "action": action, "rationale": rationale}
    if action in ("buy", "sell"):
        decision["qty"] = "0.001"
        decision["limit_price"] = "100.1"
    entry = {
        "request": _request(request_id, ts_ns),
        "decision": decision,
        "result": result,
        "mode": mode,
        "prompt_version": "v1",
        "model": "test-model",
    }
    if latency_ms is not None:
        entry["latency_ms"] = latency_ms
    if cost_usd is not None:
        # Wave 2: cost_usd is a decimal string on the wire (agent/src/types.ts); this helper
        # still takes a plain float for readability at each call site and stringifies it here,
        # once, so every fixture entry exercises ledger_report.py's real (string) input shape.
        entry["cost_usd"] = str(cost_usd)
    return entry


# 8 entries: 2 accepted buys, 3 consecutive risk-rejects (triggers the anomaly at the threshold
# of 3), 1 no_trade (result None), 1 accepted sell, 1 accepted buy whose rationale contains a
# hindsight phrase (triggers the second anomaly).
FIXTURE_ENTRIES = [
    _entry(
        "dr-1",
        1_000,
        "buy",
        {"accepted": True, "client_order_id": 1},
        latency_ms=100.0,
        cost_usd=0.01,
    ),
    _entry(
        "dr-2",
        2_000,
        "buy",
        {"accepted": False, "risk_reject": "max_notional"},
        latency_ms=50.0,
        cost_usd=0.02,
    ),
    _entry(
        "dr-3",
        3_000,
        "buy",
        {"accepted": False, "risk_reject": "max_notional"},
        latency_ms=60.0,
        cost_usd=0.02,
    ),
    _entry(
        "dr-4",
        4_000,
        "buy",
        {"accepted": False, "halted": "kill_switch"},
        latency_ms=40.0,
        cost_usd=0.02,
    ),
    _entry("dr-5", 5_000, "no_trade", None, latency_ms=20.0, cost_usd=0.0),
    _entry(
        "dr-6",
        6_000,
        "sell",
        {"accepted": True, "client_order_id": 2},
        latency_ms=200.0,
        cost_usd=0.03,
    ),
    _entry(
        "dr-7",
        7_000,
        "buy",
        {"accepted": True, "client_order_id": 3},
        rationale="in hindsight the market moved as expected",
        latency_ms=80.0,
        cost_usd=0.01,
    ),
]


def _write_ledger(tmp_path: Path, entries: list[dict]) -> Path:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return ledger


def test_action_counts_match_hand_count(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    report = ledger_report.build_report(ledger)
    # 5 buys (dr-1,2,3,4,7), 1 sell (dr-6), 1 no_trade (dr-5)
    assert report.action_counts == {"buy": 5, "no_trade": 1, "sell": 1}
    assert report.entry_count == 7


def test_accept_reject_counts_and_reasons(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    report = ledger_report.build_report(ledger)
    # accepted: dr-1, dr-6, dr-7 = 3; rejected: dr-2, dr-3, dr-4 = 3; no_result: dr-5 = 1
    assert report.accepted == 3
    assert report.rejected == 3
    assert report.no_result == 1
    assert report.risk_reject_counts == {"max_notional": 2}
    assert report.halted_counts == {"kill_switch": 1}


def test_latency_summary_hand_computed(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    report = ledger_report.build_report(ledger)
    # values: 100, 50, 60, 40, 20, 200, 80 -> n=7, sorted: 20,40,50,60,80,100,200
    # mean = 550/7 = 78.571428...; median = 60; p95 nearest-rank idx=round(0.95*6)=6 -> 200; max=200
    lat = report.latency_ms
    assert lat["n"] == 7
    assert lat["mean"] == round(550 / 7, 3)
    assert lat["median"] == 60.0
    assert lat["p95"] == 200.0
    assert lat["max"] == 200.0


def test_cost_summary_hand_computed(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    report = ledger_report.build_report(ledger)
    # total = 0.01+0.02+0.02+0.02+0.0+0.03+0.01 = 0.11; n=7 -> mean=0.11/7
    assert report.cost_usd["total"] == round(0.11, 6)
    assert report.cost_usd["mean_per_decision"] == round(0.11 / 7, 6)


def test_consecutive_rejects_anomaly_flagged_at_threshold(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    report = ledger_report.build_report(ledger)
    kinds = [a.kind for a in report.anomalies]
    assert "consecutive_risk_rejects" in kinds
    match = next(a for a in report.anomalies if a.kind == "consecutive_risk_rejects")
    assert "dr-2..dr-4" in match.detail


def test_two_consecutive_rejects_is_not_flagged(tmp_path: Path) -> None:
    # Only 2 in a row (below CONSECUTIVE_REJECT_THRESHOLD=3) must not be flagged.
    entries = [
        _entry("dr-1", 1_000, "buy", {"accepted": True}),
        _entry("dr-2", 2_000, "buy", {"accepted": False, "risk_reject": "max_notional"}),
        _entry("dr-3", 3_000, "buy", {"accepted": False, "risk_reject": "max_notional"}),
        _entry("dr-4", 4_000, "buy", {"accepted": True}),
    ]
    ledger = _write_ledger(tmp_path, entries)
    report = ledger_report.build_report(ledger)
    assert not any(a.kind == "consecutive_risk_rejects" for a in report.anomalies)


def test_hindsight_phrase_flagged(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    report = ledger_report.build_report(ledger)
    hindsight = [a for a in report.anomalies if a.kind == "possible_hindsight_reference"]
    assert len(hindsight) == 1
    assert "dr-7" in hindsight[0].detail


def test_malformed_line_is_counted_and_flagged(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(json.dumps(FIXTURE_ENTRIES[0]) + "\nnot json\n")
    report = ledger_report.build_report(ledger)
    assert report.malformed_lines == 1
    assert report.entry_count == 1
    assert any(a.kind == "malformed_lines" for a in report.anomalies)


def test_missing_ledger_produces_empty_deterministic_report(tmp_path: Path) -> None:
    report = ledger_report.build_report(tmp_path / "does-not-exist.jsonl")
    assert report.entry_count == 0
    assert report.action_counts == {}
    assert report.anomalies == []


def test_same_input_produces_byte_identical_report_json(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    json1, _ = ledger_report.write_report(ledger, out1)
    json2, _ = ledger_report.write_report(ledger, out2)
    payload1 = json.loads(json1.read_text())
    payload2 = json.loads(json2.read_text())
    del payload1["generated_at"], payload2["generated_at"]
    assert payload1 == payload2


def test_markdown_report_is_short_and_readable(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path, FIXTURE_ENTRIES)
    _, md_path = ledger_report.write_report(ledger, tmp_path / "out")
    text = md_path.read_text()
    assert "# Decision ledger report" in text
    assert "## Anomalies" in text
    # Not a raw dump: far shorter than the raw ledger would render as JSON.
    assert len(text.splitlines()) < 40
