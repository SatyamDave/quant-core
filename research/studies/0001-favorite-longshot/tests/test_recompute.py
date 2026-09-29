"""Recompute the primary metric straight from the raw JSON with separate code."""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
RAW = HERE.parents[2] / "data" / "raw" / "study-0001"


def independent_G(raw: Path) -> float:
    def sec(s: str) -> float:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    info = {}
    for f in (raw / "listing").glob("day_*.json"):
        for m in json.loads(f.read_text())["markets"]:
            info[m["ticker"]] = (sec(m["expected_expiration_time"]) - 86400, m["result"])
    total, n = 0.0, 0
    for entry in json.loads((raw / "trades" / "sampled.json").read_text())["markets"]:
        d, result = info[entry["ticker"]]
        trades = [t for f in entry["files"] for t in json.loads((raw / f).read_text())["trades"]]
        before = [t for t in trades if sec(t["created_time"]) <= d]
        if not before or sum(float(t["count_fp"]) for t in before) < 10:
            continue
        newest = max(sec(t["created_time"]) for t in before)
        p = float(next(t for t in before if sec(t["created_time"]) == newest)["yes_price_dollars"])
        y = 1.0 if result == "yes" else 0.0
        if p <= 0.15:
            total, n = total + (p - y), n + 1
        elif p >= 0.85:
            total, n = total + (y - p), n + 1
    return total / n


PRICES = (0.05, 0.1, 0.5, 0.9, 0.95)
GOLDEN = 0.6180339887498949


def write_synthetic_snapshot(raw: Path) -> None:
    """SYNTHETIC plumbing fixture: calibrated outcomes, never evidence of an edge.

    Deterministic without `random`: market i gets price PRICES[i % 5] and resolves yes when the
    golden-ratio sequence frac(i * GOLDEN), which is spread evenly over [0, 1), falls below it.
    """

    def iso(s: int) -> str:
        return datetime.fromtimestamp(s, UTC).isoformat().replace("+00:00", "Z")

    for d in ("listing", "trades/live", "series"):
        (raw / d).mkdir(parents=True)
    markets, sampled = [], []
    for e in range(200):
        base = 1_786_000_000 + e * 8000
        for k in range(3):
            i = e * 3 + k
            p = PRICES[i % len(PRICES)]
            t = f"KXS-E{e}-{k}"
            markets.append(
                {
                    "ticker": t,
                    "event_ticker": f"KXS-E{e}",
                    "market_type": "binary",
                    "result": "yes" if (i * GOLDEN) % 1.0 < p else "no",
                    "volume_fp": "100.00",
                    "expected_expiration_time": iso(base + 86400),
                    "open_time": iso(base - 86400),
                    "close_time": iso(base + 86400),
                    "settlement_ts": iso(base + 90000),
                }
            )
            trades = [
                {
                    "trade_id": t + "a",
                    "ticker": t,
                    "created_time": iso(base + 50),
                    "yes_price_dollars": "0.5",
                    "count_fp": "50.00",
                },
                {
                    "trade_id": t + "b",
                    "ticker": t,
                    "created_time": iso(base - 100),
                    "yes_price_dollars": str(p),
                    "count_fp": "20.00",
                },
            ]
            f = f"trades/live/{t}.json"
            (raw / f).write_text(json.dumps({"trades": trades}))
            sampled.append({"ticker": t, "files": [f]})
    (raw / "listing" / "day_x.json").write_text(json.dumps({"markets": markets}))
    (raw / "listing" / "manifest.json").write_text("{}")
    (raw / "series" / "series.json").write_text(
        json.dumps({"series": [{"ticker": "KXS", "fee_multiplier": 1}]})
    )
    (raw / "trades" / "sampled.json").write_text(json.dumps({"markets": sampled, "stopped": "x"}))


def test_recompute_matches_on_synthetic_fixture(tmp_path: Path) -> None:
    """Plumbing only: the two code paths agree on SYNTHETIC data. Says nothing about Kalshi."""
    import study

    write_synthetic_snapshot(tmp_path)
    res = study.analyze(study.build(tmp_path))
    assert res["primary"]["pooled_markets"] > 100
    assert independent_G(tmp_path) == pytest.approx(res["primary"]["G"], abs=1e-12)


def real_snapshot_has_sample() -> bool:
    results = HERE / "results.json"
    return results.exists() and "blocked" not in json.loads(results.read_text())


@pytest.mark.skipif(not real_snapshot_has_sample(), reason="no analysable real snapshot")
def test_primary_metric_matches_independent_recompute() -> None:
    results = json.loads((HERE / "results.json").read_text())
    assert independent_G(RAW) == pytest.approx(results["primary"]["G"], abs=1e-12)
