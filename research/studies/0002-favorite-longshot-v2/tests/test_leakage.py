# Copied with attribution from research/studies/0001-favorite-longshot/tests/test_leakage.py
# study 0001's own test file is not modified.
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import study

D = 1_750_000_000


def rows_labels() -> tuple[list[dict[str, object]], dict[str, dict[str, int]]]:
    rows, labels = [], {}
    for i in range(10):
        t = f"M{i}"
        rows.append(
            {
                "ticker": t,
                "event_ticker": f"E{i}",
                "series": "S",
                "decision_ts": D + i * 86400,
                "price": 0.1,
                "price_ts": D + i * 86400 - 60,
                "fee_multiplier": 1.0,
                "fee_waived": False,
                "window_volume": 50.0,
            }
        )
        labels[t] = {"y": 0, "settlement_ts": D + i * 86400 + 3600 * 25}
    return rows, labels


def test_clean_rows_pass() -> None:
    rows, labels = rows_labels()
    sp = study.split(rows, labels)
    assert sp["train"] and sp["test"]
    study.check_no_leakage(rows, labels, sp)


def test_price_after_decision_trips() -> None:
    rows, labels = rows_labels()
    rows[3]["price_ts"] = rows[3]["decision_ts"] + 1
    with pytest.raises(AssertionError, match="price after decision"):
        study.check_no_leakage(rows, labels, study.split(rows, labels))


def test_label_in_feature_row_trips() -> None:
    rows, labels = rows_labels()
    rows[0]["result"] = "yes"
    with pytest.raises(AssertionError, match="non-feature field"):
        study.check_no_leakage(rows, labels, study.split(rows, labels))


def test_train_settling_after_test_decision_trips() -> None:
    rows, labels = rows_labels()
    sp = study.split(rows, labels)
    first_test = min(r["decision_ts"] for r in rows if r["event_ticker"] in sp["test"])
    labels[f"M{sp['train'][0][1:]}"]["settlement_ts"] = first_test + 1
    with pytest.raises(AssertionError, match="train labels settle after"):
        study.check_no_leakage(rows, labels, sp)


def test_feature_row_ignores_trades_after_decision() -> None:
    m = {"ticker": "T", "event_ticker": "S-E", "expected_expiration_time": "2026-08-10T00:00:00Z"}
    d = study.decision_ts(m)
    iso = lambda s: datetime.fromtimestamp(s, UTC).isoformat()  # noqa: E731
    trades = [
        {"created_time": iso(d + 5), "yes_price_dollars": "0.99", "count_fp": "100"},
        {"created_time": iso(d - 5), "yes_price_dollars": "0.10", "count_fp": "20"},
    ]
    row, reason = study.feature_row(m, trades, {})
    assert reason == "ok" and row is not None
    assert row["price"] == 0.10 and row["price_ts"] <= row["decision_ts"]
