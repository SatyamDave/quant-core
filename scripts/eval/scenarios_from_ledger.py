#!/usr/bin/env python3
"""Issue #52: turn real decisions into new frozen test scenarios ("recursive learning").

  scripts/eval/scenarios_from_ledger.py [--ledger PATH] [--out-dir DIR] [--horizon N] [--root DIR]

Reads a decision ledger (schema `schemas/decision/v1/decision_ledger_entry.schema.json`, e.g.
`out/agent/ledger.jsonl` or an archived Tier 2 / shadow-run copy) and, for every entry whose
outcome is now known, checks it against three documented selection rules (see `classify()`):

  - `risk_rejected`  — a buy/sell the risk engine blocked. Interesting because it's the exact
    case that proves (or disproves) the safety check mattered.
  - `confident_loss` — an accepted buy/sell the decider was confident about (`confidence` at or
    above the fake decider's own threshold, `decider/fake.ts`'s `DEFAULT_PROB_THRESHOLD`) that
    lost money by the mark below.
  - `profitable_no_trade` — a declined request where a reference position in the direction the
    market actually went would have made money by the mark below.

An entry's outcome is "known" only when at least `--horizon` (default 20) further entries exist
later in the same ledger: their `request.mid` marks the P&L computed above. An entry within
`--horizon` of the end of the ledger has no future price yet, so it is never selected — this is
a real constraint (root CLAUDE.md rule 8: no lookahead), not a tuning knob.

Each selected entry becomes one new scenario under `--out-dir` (default
`evals/scenarios/from_ledger/`): a single-snapshot recording (`record.rs` format, same as
`evals/scenarios/worldline-*.csv`) that reproduces the entry's own recorded top-of-book exactly,
plus a `.provenance.json` sidecar with the source ledger's sha256, the entry's index and
`request_id`, the ledger's overall date range, the selection reason and evidence, the recorded
outcome, and (for human audit only) the original decision/result. `build_snapshot_csv()` reads
only the entry's `request` — never `decision`/`result`/`cost_usd` — so the recorded outcome
cannot leak into the generated scenario's decision inputs by construction; see
`tests/evals/test_scenarios_from_ledger.py::test_outcome_never_leaks_into_scenario_csv`.

## Known limitation (recorded, not hidden)

A single ledger entry does not carry the order-book/tick history the classifier's `signal` and
`features` were computed from, and that history cannot be honestly reconstructed after the fact.
A consumer that replays the generated snapshot through the real `qc-bridge`/`qc-inference`
pipeline will therefore get a *freshly computed* `signal`, generally different from the one
originally recorded — the scenario is faithful to the recorded top-of-book (bid/ask/spread),
never to the recorded signal. Every scenario's header comment and provenance file say so, and
the original recorded signal/decision/result are kept in the provenance file for a human to
compare directly, not silently discarded.

Labelled `"synthetic"` when the source ledger entry's `mode` is `"fake"`/`"replay"`, `"real"`
when `"live"` (a real paper or tiny-live decision, per issue #52's user flow — `"live"` mode
still only ever routes to `SimVenue` per ADR-0040 until a real broker adapter is configured).

Idempotent: re-running over the same ledger produces the same files again (skipped, not
duplicated); a slug collision with *different* content is a hard error, matching
`scripts/reports/pnl_report.py`'s append-only convention (root CLAUDE.md rule 10).

Not wired into `scripts/eval/live_agent.py`'s paid Tier 2 scenario set (`worldline-*.csv` only,
by design there) or into `scripts/eval/run.py`'s free Tier 1 cases — a human reviews generated
scenarios before they're added to either. `scripts/eval/promote.py`'s frozen suite (Tier 0, free,
fake mode) does include this directory by default: that's the loop issue #52 exists to close.

Standard library only, matching every other script in scripts/eval and scripts/reports.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "out/agent/ledger.jsonl"
DEFAULT_OUT_DIR = ROOT / "evals/scenarios/from_ledger"
DEFAULT_POLICY = ROOT / "scripts/reports/pnl_policy.toml"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "reports"))
import pnl_report  # noqa: E402

# Selection thresholds: heuristics for "interesting enough to become a test case" (issue #52's
# third acceptance criterion — documented once, here, not decided differently per run).
HORIZON_ENTRIES = 20
# Matches decider/fake.ts's own DEFAULT_PROB_THRESHOLD: the same bar the fake decider itself uses
# to call a signal "confident" is the bar used here to call a loss on it interesting.
CONFIDENT_LOSS_MIN_CONFIDENCE = 0.6
LEVELS_PER_SIDE = 3
DEFAULT_LEVEL_QTY = Decimal("1")
DEFAULT_TICK = Decimal("0.01")

REASON_RISK_REJECTED = "risk_rejected"
REASON_CONFIDENT_LOSS = "confident_loss"
REASON_PROFITABLE_NO_TRADE = "profitable_no_trade"


@dataclass(frozen=True)
class SelectedEntry:
    index: int
    request_id: str
    reason: str
    detail: str
    outcome_usd: str
    mark_ts_ns: int
    mark_mid: str


def _isolated_pnl(
    side: str, qty: Decimal, entry_price: Decimal, mark_price: Decimal, fee_bps: Decimal
) -> Decimal:
    """P&L of one hypothetical fill, entered fresh (no pre-existing position) at `entry_price`
    and marked at `mark_price` — reuses `pnl_report`'s own fill accounting rather than
    reimplementing it. This is a single-decision estimate, not a portfolio replay: it does not
    account for the ledger's running position across other decisions."""
    pos = pnl_report.apply_fill(pnl_report.Position(), side, qty, entry_price, fee_bps)
    return pos.total_pnl_after_fees(mark_price)


def known_outcome_indices(entry_count: int, horizon: int) -> range:
    """Indices with at least `horizon` later entries in the same ledger to mark against. An
    entry without that many future entries has no known outcome yet (root CLAUDE.md rule 8: no
    lookahead) — it is simply not yet eligible, not a failure."""
    return range(0, max(0, entry_count - horizon))


def classify(
    entries: list[dict[str, Any]], i: int, horizon: int, fee_bps: Decimal
) -> SelectedEntry | None:
    """Applies the three documented selection rules to `entries[i]`, marking against
    `entries[i + horizon]`'s `request.mid`. Returns the first rule that matches, or None."""
    entry = entries[i]
    request = entry["request"]
    decision = entry["decision"]
    result = entry.get("result")
    mark_request = entries[i + horizon]["request"]
    mark_mid = Decimal(str(mark_request["mid"]))
    mark_ts_ns = int(mark_request["ts_ns"])
    decision_mid = Decimal(str(request["mid"]))
    action = decision.get("action")

    if action in ("buy", "sell"):
        qty = Decimal(str(decision["qty"]))
        price = Decimal(str(decision["limit_price"]))
        pnl = _isolated_pnl(action, qty, price, mark_mid, fee_bps)

        if isinstance(result, dict) and result.get("accepted") is False:
            reject_reason = result.get("risk_reject") or result.get("halted") or "unknown"
            return SelectedEntry(
                i,
                decision["request_id"],
                REASON_RISK_REJECTED,
                f"risk rejected ({reject_reason}); had it filled, the {horizon}-entry-forward "
                f"P&L would have been ${pnl}",
                str(pnl),
                mark_ts_ns,
                str(mark_mid),
            )

        confidence = decision.get("confidence")
        if (
            isinstance(result, dict)
            and result.get("accepted") is True
            and isinstance(confidence, (int, float))
            and confidence >= CONFIDENT_LOSS_MIN_CONFIDENCE
            and pnl < 0
        ):
            return SelectedEntry(
                i,
                decision["request_id"],
                REASON_CONFIDENT_LOSS,
                f"confidence {confidence:.4f} >= {CONFIDENT_LOSS_MIN_CONFIDENCE}, but the "
                f"{horizon}-entry-forward P&L was ${pnl}",
                str(pnl),
                mark_ts_ns,
                str(mark_mid),
            )
        return None

    if action == "no_trade":
        max_position = Decimal(str(request.get("limits", {}).get("max_position", "0")))
        qty = max_position if max_position > 0 else DEFAULT_LEVEL_QTY
        if mark_mid > decision_mid:
            side = "buy"
        elif mark_mid < decision_mid:
            side = "sell"
        else:
            return None
        pnl = _isolated_pnl(side, qty, decision_mid, mark_mid, fee_bps)
        if pnl > 0:
            return SelectedEntry(
                i,
                decision["request_id"],
                REASON_PROFITABLE_NO_TRADE,
                f"declined; a reference {side} of size {qty} entered at mid {decision_mid} and "
                f"marked at the {horizon}-entry-forward mid {mark_mid} would have made ${pnl}",
                str(pnl),
                mark_ts_ns,
                str(mark_mid),
            )
    return None


def build_snapshot_csv(request: dict[str, Any]) -> str:
    """One `record.rs`-format snapshot line reproducing `request`'s own top-of-book. Takes only
    `request` (never the entry's `decision`/`result`/`cost_usd`/etc.) — this is what makes the
    recorded outcome unable to leak into the generated scenario's decision inputs, by
    construction rather than by filtering (tested directly:
    test_outcome_never_leaks_into_scenario_csv)."""
    instrument = str(request["instrument"])
    ts_ns = int(request["ts_ns"])
    best_bid = Decimal(str(request["best_bid"]))
    best_ask = Decimal(str(request["best_ask"]))
    spread_ticks = int(request.get("spread_ticks") or 1) or 1
    tick = (best_ask - best_bid) / spread_ticks
    if tick <= 0:
        tick = DEFAULT_TICK
    bids = ";".join(f"{best_bid - i * tick}@{DEFAULT_LEVEL_QTY}" for i in range(LEVELS_PER_SIDE))
    asks = ";".join(f"{best_ask + i * tick}@{DEFAULT_LEVEL_QTY}" for i in range(LEVELS_PER_SIDE))
    seq = 1000
    return f"S,{instrument},{seq},{ts_ns},{ts_ns},{bids},{asks}\n"


def csv_header(selected: SelectedEntry, label: str, provenance_filename: str) -> str:
    return (
        f"# derived-from-ledger scenario (issue #52), label={label}: reproduces the recorded "
        f"top-of-book of one real logged decision (request_id={selected.request_id}, "
        f"reason={selected.reason}). A single frozen snapshot, not a sliced order-book history: "
        f"qc-bridge/qc-inference recompute `signal`/`features` fresh from this book alone, which "
        f"will generally NOT match the originally recorded signal (feature history cannot be "
        f"honestly reconstructed from one ledger entry) — see {provenance_filename} for the "
        f"original recorded decision, result and outcome.\n"
    )


def _slug(ledger_hash: str, request_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", request_id)
    return f"ledger-{ledger_hash[:12]}-{safe_id}"


def ledger_date_range(entries: list[dict[str, Any]]) -> dict[str, Any]:
    stamps = [int(e["request"]["ts_ns"]) for e in entries if isinstance(e.get("request"), dict)]
    if not stamps:
        return {"first_ts_ns": None, "last_ts_ns": None, "first_iso": None, "last_iso": None}
    lo, hi = min(stamps), max(stamps)
    iso = lambda ns: dt.datetime.fromtimestamp(ns / 1e9, tz=dt.UTC).isoformat()  # noqa: E731
    return {"first_ts_ns": lo, "last_ts_ns": hi, "first_iso": iso(lo), "last_iso": iso(hi)}


def build_provenance(
    selected: SelectedEntry,
    entry: dict[str, Any],
    label: str,
    ledger_path: Path,
    ledger_hash: str,
    date_range: dict[str, Any],
    scenario_filename: str,
    horizon: int,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "issue": 52,
        "scenario_csv": scenario_filename,
        "label": label,
        "selection": {"reason": selected.reason, "detail": selected.detail},
        "source_ledger": {
            "path": str(ledger_path),
            "sha256": ledger_hash,
            "entry_index": selected.index,
            "request_id": selected.request_id,
            "date_range": date_range,
        },
        "outcome": {
            "mark_ts_ns": selected.mark_ts_ns,
            "mark_mid": selected.mark_mid,
            "pnl_usd": selected.outcome_usd,
            "horizon_entries": horizon,
        },
        # For human audit only: never fed back into build_snapshot_csv / the generated scenario.
        "original": {
            "decision": entry["decision"],
            "result": entry.get("result"),
            "mode": entry.get("mode"),
            "prompt_version": entry.get("prompt_version"),
            "model": entry.get("model"),
        },
    }


def generate(
    ledger_path: Path,
    out_dir: Path = DEFAULT_OUT_DIR,
    policy_path: Path = DEFAULT_POLICY,
    horizon: int = HORIZON_ENTRIES,
) -> list[Path]:
    """Returns the list of newly written scenario CSV paths (already-generated, identical
    scenarios are skipped, not counted as new)."""
    entries = pnl_report.load_entries(ledger_path)
    if not entries:
        return []
    policy = pnl_report.load_policy(policy_path)
    fee_bps = Decimal(str(policy["fees"]["fee_bps"]))
    ledger_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    date_range = ledger_date_range(entries)

    written: list[Path] = []
    for i in known_outcome_indices(len(entries), horizon):
        selected = classify(entries, i, horizon, fee_bps)
        if selected is None:
            continue
        label = "real" if entries[i].get("mode") == "live" else "synthetic"
        slug = _slug(ledger_hash, selected.request_id)
        csv_path = out_dir / f"{slug}.csv"
        prov_path = out_dir / f"{slug}.provenance.json"

        provenance = build_provenance(
            selected,
            entries[i],
            label,
            ledger_path,
            ledger_hash,
            date_range,
            csv_path.name,
            horizon,
        )
        prov_text = json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        csv_text = csv_header(selected, label, prov_path.name) + build_snapshot_csv(
            entries[i]["request"]
        )

        if prov_path.exists():
            if prov_path.read_text() != prov_text:
                raise RuntimeError(
                    f"{prov_path} already exists with different content for the same slug "
                    f"({selected.request_id}) — generated scenarios are append-only, matching "
                    "scripts/reports/pnl_report.py's convention (root CLAUDE.md rule 10)"
                )
            continue  # identical: already generated, nothing to do

        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(csv_text)
        prov_path.write_text(prov_text)
        written.append(csv_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--ledger", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--horizon", type=int, default=HORIZON_ENTRIES)
    args = parser.parse_args(argv)

    ledger = args.ledger or (args.root / "out/agent/ledger.jsonl")
    out_dir = args.out_dir or (args.root / "evals/scenarios/from_ledger")
    policy = args.policy or (args.root / "scripts/reports/pnl_policy.toml")

    if not ledger.exists():
        print(f"no ledger at {ledger}; nothing to do (this is expected before any decisions exist)")
        return 0

    written = generate(ledger, out_dir, policy, args.horizon)
    print(f"{len(written)} new scenario(s) written to {out_dir}")
    for path in written:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
