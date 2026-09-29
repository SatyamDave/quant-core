#!/usr/bin/env python3
"""Issue #39: render the agent's decision ledger into a short, readable report.

  scripts/reports/ledger_report.py [--ledger PATH] [--out DIR]

Reads a decision ledger (`out/agent/ledger.jsonl` by default; schema
`schemas/decision/v1/decision_ledger_entry.schema.json`) and writes a deterministic
`report.json` and `report.md` (both under --out, default `out/reports/ledger`, both gitignored
like every other run artifact under out/): counts by action, the risk gate's accept/reject rates
and reasons, decision latency, AI cost, and a list of anomalies (a run of consecutive risk
rejects, a malformed line, or a decision whose rationale reads like it saw something outside its
own `DecisionRequest`). Same input always produces the same output — no wall-clock value feeds
into any computed number, only into the `generated_at` metadata field.

Standard library only, matching scripts/eval/run.py's own convention.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "out/agent/ledger.jsonl"
DEFAULT_OUT = ROOT / "out/reports/ledger"

# A run of this many or more consecutive risk-rejected/halted decisions is flagged: not
# necessarily a bug, but exactly the kind of pattern a human reviewing the ledger should see
# named rather than have to notice by eye in a raw dump (issue #39's "flags anything odd").
CONSECUTIVE_REJECT_THRESHOLD = 3

# ponytail: a phrase list is a heuristic, not a proof of contamination — it will miss a
# hindsight-flavored rationale phrased unusually and can false-positive on an innocent use of one
# of these words. Upgrade path: the Shapley-attributed "fraction of decision-driving reasoning
# that is contaminated" metric from arXiv:2602.17234 (llm-trading-eval-research.md §1), once that
# kind of per-claim attribution is worth building here.
HINDSIGHT_PHRASES = (
    "in hindsight",
    "with the benefit of hindsight",
    "as we now know",
    "as it turned out",
    "it turned out",
    "later confirmed",
    "subsequently",
    "afterward we learned",
    "we now know",
    "in retrospect",
)


@dataclass
class Anomaly:
    kind: str
    detail: str


@dataclass
class LedgerReport:
    entry_count: int
    malformed_lines: int
    action_counts: dict[str, int]
    accepted: int
    rejected: int
    no_result: int
    risk_reject_counts: dict[str, int]
    halted_counts: dict[str, int]
    latency_ms: dict[str, float | int]
    cost_usd: dict[str, Any]
    anomalies: list[Anomaly] = field(default_factory=list)


def _load_entries(ledger: Path) -> tuple[list[dict[str, Any]], int]:
    """Returns (parsed entries in file order, count of lines that failed to parse as JSON)."""
    if not ledger.exists():
        return [], 0
    entries: list[dict[str, Any]] = []
    malformed = 0
    for line in ledger.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1
    return entries, malformed


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile, deterministic and dependency-free (no numpy)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return ordered[idx]


def _latency_summary(entries: list[dict[str, Any]]) -> dict[str, float | int]:
    values = [
        float(e["latency_ms"]) for e in entries if isinstance(e.get("latency_ms"), (int, float))
    ]
    if not values:
        return {"n": 0, "mean": 0.0, "median": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3),
    }


def _cost_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, float] = {}
    total = 0.0
    n = 0
    for e in entries:
        cost = e.get("cost_usd")
        # Wave 2: cost_usd is now a decimal string; a bare number is still accepted for any
        # pre-wave-2 fixture/ledger line. This report's own numbers stay floats (informational
        # summary stats, not a money-accounting path -- pnl_report.py is the one that must use
        # Decimal throughout, per its own docstring).
        if not isinstance(cost, (int, float, str)):
            continue
        try:
            cost_f = float(cost)
        except ValueError:
            continue  # a malformed cost_usd must not take the whole report down
        mode = e.get("mode", "unknown")
        by_mode[mode] = by_mode.get(mode, 0.0) + cost_f
        total += cost_f
        n += 1
    return {
        "total": round(total, 6),
        "mean_per_decision": round(total / n, 6) if n else 0.0,
        "by_mode": {k: round(v, 6) for k, v in sorted(by_mode.items())},
    }


def _request_id(entry: dict[str, Any]) -> str:
    return str(
        entry.get("decision", {}).get("request_id")
        or entry.get("request", {}).get("request_id")
        or "?"
    )


def _is_rejected(entry: dict[str, Any]) -> bool:
    result = entry.get("result")
    return isinstance(result, dict) and result.get("accepted") is False


def _find_consecutive_rejects(entries: list[dict[str, Any]]) -> list[Anomaly]:
    anomalies = []
    run_start = None
    for i, entry in enumerate(entries):
        if _is_rejected(entry):
            if run_start is None:
                run_start = i
            continue
        if run_start is not None:
            length = i - run_start
            if length >= CONSECUTIVE_REJECT_THRESHOLD:
                ids = [_request_id(entries[j]) for j in range(run_start, i)]
                anomalies.append(
                    Anomaly(
                        "consecutive_risk_rejects",
                        f"{length} consecutive rejects: {ids[0]}..{ids[-1]}",
                    )
                )
            run_start = None
    if run_start is not None:
        length = len(entries) - run_start
        if length >= CONSECUTIVE_REJECT_THRESHOLD:
            ids = [_request_id(entries[j]) for j in range(run_start, len(entries))]
            anomalies.append(
                Anomaly(
                    "consecutive_risk_rejects",
                    f"{length} consecutive rejects (through end of ledger): {ids[0]}..{ids[-1]}",
                )
            )
    return anomalies


def _find_hindsight_flags(entries: list[dict[str, Any]]) -> list[Anomaly]:
    anomalies = []
    for entry in entries:
        rationale = str(entry.get("decision", {}).get("rationale") or "")
        lowered = rationale.lower()
        hit = next((p for p in HINDSIGHT_PHRASES if p in lowered), None)
        if hit:
            anomalies.append(
                Anomaly(
                    "possible_hindsight_reference",
                    f"{_request_id(entry)}: rationale contains {hit!r} "
                    "(heuristic phrase match, not proof — see HINDSIGHT_PHRASES)",
                )
            )
    return anomalies


def _find_malformed(malformed_lines: int) -> list[Anomaly]:
    if malformed_lines:
        return [Anomaly("malformed_lines", f"{malformed_lines} line(s) were not valid JSON")]
    return []


def build_report(ledger: Path) -> LedgerReport:
    entries, malformed = _load_entries(ledger)
    action_counts = Counter(e.get("decision", {}).get("action", "?") for e in entries)
    results = [e.get("result") for e in entries]
    accepted = sum(1 for r in results if isinstance(r, dict) and r.get("accepted") is True)
    rejected = sum(1 for r in results if isinstance(r, dict) and r.get("accepted") is False)
    no_result = sum(1 for r in results if r is None)
    risk_reject_counts = Counter(
        r["risk_reject"] for r in results if isinstance(r, dict) and "risk_reject" in r
    )
    halted_counts = Counter(r["halted"] for r in results if isinstance(r, dict) and "halted" in r)

    anomalies = (
        _find_malformed(malformed)
        + _find_consecutive_rejects(entries)
        + _find_hindsight_flags(entries)
    )

    return LedgerReport(
        entry_count=len(entries),
        malformed_lines=malformed,
        action_counts=dict(sorted(action_counts.items())),
        accepted=accepted,
        rejected=rejected,
        no_result=no_result,
        risk_reject_counts=dict(sorted(risk_reject_counts.items())),
        halted_counts=dict(sorted(halted_counts.items())),
        latency_ms=_latency_summary(entries),
        cost_usd=_cost_summary(entries),
        anomalies=anomalies,
    )


def render_markdown(report: LedgerReport, ledger_path: Path, generated_at: str) -> str:
    lines = [
        "# Decision ledger report",
        "",
        f"Generated {generated_at} from `{ledger_path}` ({report.entry_count} entries, "
        f"{report.malformed_lines} malformed line(s) skipped).",
        "",
        "## Actions",
        "",
        "| Action | Count |",
        "|---|---|",
    ]
    if report.action_counts:
        lines += [f"| {action} | {count} |" for action, count in report.action_counts.items()]
    else:
        lines.append("| (none) | 0 |")

    order_attempts = report.accepted + report.rejected
    accept_pct = (report.accepted / order_attempts * 100) if order_attempts else 0.0
    reject_pct = (report.rejected / order_attempts * 100) if order_attempts else 0.0
    lines += [
        "",
        "## Risk gate",
        "",
        f"- Accepted: {report.accepted} ({accept_pct:.1f}% of order attempts)",
        f"- Rejected: {report.rejected} ({reject_pct:.1f}%)",
    ]
    for reason, count in report.risk_reject_counts.items():
        lines.append(f"  - {reason}: {count}")
    for reason, count in report.halted_counts.items():
        lines.append(f"  - halted/{reason}: {count}")
    lines.append(f"- No result recorded (e.g. no_trade): {report.no_result}")

    lat = report.latency_ms
    lines += [
        "",
        "## Latency (ms)",
        "",
        f"- n={lat['n']}, mean={lat['mean']}, median={lat['median']}, "
        f"p95={lat['p95']}, max={lat['max']}",
    ]

    cost = report.cost_usd
    lines += [
        "",
        "## Cost (USD)",
        "",
        f"- total={cost['total']}, mean per decision={cost['mean_per_decision']}, "
        f"by mode={cost['by_mode']}",
    ]

    lines += ["", "## Anomalies", ""]
    if report.anomalies:
        for a in report.anomalies:
            lines.append(f"- **{a.kind}**: {a.detail}")
    else:
        lines.append("None found.")
    lines.append("")
    return "\n".join(lines)


def write_report(ledger: Path, out_dir: Path) -> tuple[Path, Path]:
    report = build_report(ledger)
    generated_at = dt.datetime.now(dt.UTC).isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": generated_at, "ledger": str(ledger), **asdict(report)}
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n")
    md_path = out_dir / "report.md"
    md_path.write_text(render_markdown(report, ledger, generated_at))
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    json_path, md_path = write_report(args.ledger, args.out)
    print(md_path.read_text())
    print(f"(written to {json_path} and {md_path})")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
