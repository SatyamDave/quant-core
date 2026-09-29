#!/usr/bin/env python3
"""Issue #70: track honest profit, and know when to stop.

  scripts/reports/pnl_report.py [--ledger PATH] [--policy PATH] [--date YYYY-MM-DD] [--out DIR]
      [--source {decision_ledger,broker}] [--broker-journal PATH]

Computes realized + unrealized P&L after fees next to two baselines (no-trade, buy-and-hold),
cumulative drawdown, and AI cost — then evaluates a stop rule read from
`scripts/reports/pnl_policy.toml` (placeholder thresholds; see
that file's own docstring). Writes one append-only file per day to
`fund/track-record/daily/<date>.json` (fund/track-record/README.md documents the format);
`--date` picks which trading day this run is for (default: the ledger's last entry's date, UTC).

Money math is `decimal.Decimal` throughout — never a float (root CLAUDE.md: no floats for money).

## Two sources, honestly labelled (issue #45 closes the gap between them)

`--source decision_ledger` (the default, unchanged behaviour): every accepted buy/sell decision
is treated as fully filled at its own `limit_price` and `qty` — an *assumption*, not a real venue
fill. `source` in the written record is `"decision_ledger_estimate"`.

`--source broker` (new): reads the gateway's broker journal (`agent/src/broker/journal.ts`'s
output, default `out/agent/broker-journal.jsonl`) and, for each accepted decision whose order
the journal has resolved to `"filled"`/`"partially_filled"`, uses the broker's
own `filled_qty`/`avg_price` — what the broker actually reported via `report_execution` — instead
of the decision's estimate. A decision whose order was rejected, canceled, or never resolved by
the broker contributes no fill at all (not a zero-price one); `fills` in the written record counts
`accepted_orders` vs `matched_fills` so a lagging or incomplete journal is visible, not silently
papered over. A decision is matched to its journal entries by the order's `ref_id`, derived from
the approval payload the ledger records, never by `client_order_id` alone: qc-bridge restarts
`client_order_id` at 0 in every process, so the same id names a different order in every
bridge session the journal spans. `source` in the written record is `"reconciled"` (matching
fund/track-record/README.md's documented contract), and `reconciliation` optionally carries the
day's broker reconciliation-check outcome when the caller (`scripts/ops/daily_close.py`) supplies
one — this script itself does not talk to a broker or a bridge.

Standard library only, matching every other script in scripts/eval and scripts/reports.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = ROOT / "out/agent/ledger.jsonl"
DEFAULT_POLICY = Path(__file__).resolve().parent / "pnl_policy.toml"
DEFAULT_OUT = ROOT / "fund/track-record/daily"
DEFAULT_BROKER_JOURNAL = ROOT / "out/agent/broker-journal.jsonl"
SOURCE_DECISION_LEDGER = "decision_ledger_estimate"
SOURCE_RECONCILED = (
    "reconciled"  # fund/track-record/README.md: the label once #45's output feeds this
)
# agent/src/broker/adapter.ts's REF_ID_NAMESPACE: ref_id is the UUIDv5 of the approval payload.
REF_ID_NAMESPACE = uuid.UUID("6f1c1d0e-8a3b-4c7e-9d2f-5a6b7c8d9e0f")
SOURCE = SOURCE_DECISION_LEDGER  # back-compat alias: scripts/eval/{live_agent,promote,scenarios}.py

# A fill resolver takes one decision-ledger entry and returns (side, qty, price) if it produced a
# real fill, else None -- the one seam between "what filled" and the P&L math in compute(), so
# both sources below share exactly one average-cost engine (apply_fill).
FillResolver = Callable[[dict[str, Any]], "tuple[str, Decimal, Decimal] | None"]


@dataclass
class Position:
    """Average-cost accounting for one instrument. `qty` is signed: positive is long, negative is
    short. All fields are `Decimal`; this is intentionally the entire piece of state a fill needs
    to update (see `apply_fill`)."""

    qty: Decimal = Decimal(0)
    avg_cost: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)

    def unrealized_pnl(self, mark: Decimal) -> Decimal:
        if self.qty == 0:
            return Decimal(0)
        if self.qty > 0:
            return (mark - self.avg_cost) * self.qty
        return (self.avg_cost - mark) * -self.qty

    def total_pnl_after_fees(self, mark: Decimal) -> Decimal:
        return self.realized_pnl - self.fees_paid + self.unrealized_pnl(mark)


def apply_fill(
    pos: Position, side: str, qty: Decimal, price: Decimal, fee_bps: Decimal
) -> Position:
    """One fill's effect on a `Position`, average-cost method. `side` is `"buy"` or `"sell"`.
    A fill that flips the position (sells more than the current long, or buys more than the
    current short) realizes P&L on the closed portion at `price`, then opens the remainder as a
    new position at `price`. The fee (bps of notional) is always realized immediately, regardless
    of direction — it is a cash cost, not a mark-to-market one."""
    signed = qty if side == "buy" else -qty
    fee = qty * price * fee_bps / Decimal(10000)
    new_qty = pos.qty + signed
    same_direction = pos.qty == 0 or (pos.qty > 0) == (signed > 0)
    if same_direction:
        total_cost = pos.avg_cost * abs(pos.qty) + price * qty
        avg_cost = total_cost / abs(new_qty) if new_qty != 0 else Decimal(0)
        realized_delta = Decimal(0)
    else:
        closing_qty = min(qty, abs(pos.qty))
        if pos.qty > 0:
            realized_delta = (price - pos.avg_cost) * closing_qty
        else:
            realized_delta = (pos.avg_cost - price) * closing_qty
        avg_cost = price if qty > abs(pos.qty) else pos.avg_cost
    return Position(
        qty=new_qty,
        avg_cost=avg_cost,
        realized_pnl=pos.realized_pnl + realized_delta - fee,
        fees_paid=pos.fees_paid + fee,
    )


def load_entries(ledger: Path) -> list[dict[str, Any]]:
    if not ledger.exists():
        return []
    entries = []
    for line in ledger.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def ref_id_for_approval(payload: str) -> str:
    """Same value as `agent/src/broker/adapter.ts`'s `refIdForApproval()`."""
    return str(uuid.uuid5(REF_ID_NAMESPACE, payload))


def load_broker_journal(path: Path) -> dict[str, dict[str, Any]]:
    """Mirrors `agent/src/broker/journal.ts`'s `replayJournal()`, keyed by `ref_id` (unique per
    approved order across bridge sessions, unlike `client_order_id`): the last entry for a given
    `ref_id` wins, and only a `"resolved"` entry (a real `PlaceOrderResult`) is kept -- an
    `"attempt"` with no later `"resolved"` line means the broker never confirmed an outcome for
    that order, which this resolves to "no fill" (dropping it), never "filled." A corrupt or
    unrecognized line, including one without a `ref_id`, fails closed (raises) rather than
    silently under-counting fills, matching the TS reader and root CLAUDE.md rule 12."""
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        client_order_id = entry.get("client_order_id")
        if not isinstance(client_order_id, int):
            raise ValueError(
                f"{path}: journal entry missing an integer client_order_id: {line[:200]}"
            )
        ref_id = entry.get("ref_id")
        if not isinstance(ref_id, str):
            raise ValueError(f"{path}: journal entry missing a string ref_id: {line[:200]}")
        phase = entry.get("phase")
        if phase == "attempt":
            out.pop(ref_id, None)  # supersede any earlier resolved line: outcome unknown again
        elif phase == "resolved" and isinstance(entry.get("result"), dict):
            out[ref_id] = entry["result"]
        else:
            raise ValueError(f"{path}: journal entry has an unrecognized shape: {line[:200]}")
    return out


def _decision_ledger_fill(entry: dict[str, Any]) -> tuple[str, Decimal, Decimal] | None:
    """Default resolver: an accepted buy/sell decision is treated as filled in full at its own
    proposed qty/limit_price -- the pre-#45 assumption (module docstring)."""
    decision = entry.get("decision", {})
    result = entry.get("result")
    action = decision.get("action")
    if action in ("buy", "sell") and isinstance(result, dict) and result.get("accepted") is True:
        return action, Decimal(str(decision["qty"])), Decimal(str(decision["limit_price"]))
    return None


def make_broker_fill_resolver(journal: dict[str, dict[str, Any]]) -> FillResolver:
    """Resolver backing `--source broker`: only an order the journal has resolved to a
    real fill (`"filled"`, `"partially_filled"`, or `"canceled"` after a partial fill, with a
    nonzero `filled_qty` and an `avg_price`) counts -- a rejected, unfilled canceled, or
    still-unresolved order contributes nothing, never a zero or an assumed fill. The order is
    found by the `ref_id` of the approval the ledger entry records; an entry without one (sim
    mode) has no broker order and contributes nothing."""

    def resolve(entry: dict[str, Any]) -> tuple[str, Decimal, Decimal] | None:
        decision = entry.get("decision", {})
        result = entry.get("result")
        action = decision.get("action")
        if action not in ("buy", "sell"):
            return None
        if not isinstance(result, dict) or result.get("accepted") is not True:
            return None
        approval = result.get("approval")
        payload = approval.get("payload") if isinstance(approval, dict) else None
        if not isinstance(payload, str):
            return None
        resolved = journal.get(ref_id_for_approval(payload))
        # "canceled" too: the gateway emulates IOC as gfd + cancel, so a partial fill ends
        # canceled with a nonzero filled_qty.
        if resolved is None or resolved.get("status") not in (
            "filled",
            "partially_filled",
            "canceled",
        ):
            return None
        filled_qty, avg_price = resolved.get("filled_qty"), resolved.get("avg_price")
        if not filled_qty or not avg_price or Decimal(str(filled_qty)) == 0:
            return None
        return action, Decimal(str(filled_qty)), Decimal(str(avg_price))

    return resolve


def _is_accepted_trade(entry: dict[str, Any]) -> bool:
    decision = entry.get("decision", {})
    result = entry.get("result")
    return (
        decision.get("action") in ("buy", "sell")
        and isinstance(result, dict)
        and result.get("accepted") is True
    )


def _day(ts_ns: int) -> str:
    return dt.datetime.fromtimestamp(ts_ns / 1e9, tz=dt.UTC).date().isoformat()


@dataclass
class EquityPoint:
    day: str
    ts_ns: int
    agent_equity: Decimal
    bh_equity: Decimal | None


@dataclass
class PnLResult:
    source: str
    entry_count: int
    agent: dict[str, str]
    no_trade: dict[str, str]
    buy_and_hold: dict[str, str] | None
    max_drawdown_usd: str
    ai_cost_usd: str
    fills: dict[str, int]
    stop_rule: dict[str, Any]
    # Only meaningful for source="reconciled"; set by the caller (daily_close.py), never computed
    # here (this module never talks to a broker or a bridge itself). None means "not checked by
    # this run" -- never coerced into "ok" (root CLAUDE.md rule 10: neither side silently trusted).
    reconciliation: dict[str, Any] | None = None


def compute(
    entries: list[dict[str, Any]],
    fee_bps: Decimal,
    resolve_fill: FillResolver = _decision_ledger_fill,
) -> tuple[PnLResult, list[EquityPoint]]:
    """Two passes over `entries`, both cheap and both needed for a *fixed* buy-and-hold size:
    pass 1 replays every fill to find the agent's peak absolute position (the buy-and-hold
    baseline is sized to this — see module docstring: no arbitrary reference size is invented);
    pass 2 marks the agent's position and a buy-and-hold position (entered once, at the first
    entry's mid, for that same size) to every entry's own mid, building both equity curves in
    lockstep for the drawdown and stop-rule checks below.

    `resolve_fill` is the one seam between "what filled" (an assumed decision-ledger fill, or a
    real broker-journal fill) and this accounting; it defaults to the pre-#45 assumption so every
    existing caller (this module's own `main`, `scripts/eval/{live_agent,promote,
    scenarios_from_ledger}.py`) is unchanged."""
    pos = Position()
    position_curve: list[Position] = []
    peak_abs_qty = Decimal(0)
    ai_cost = Decimal(0)
    accepted_orders = 0
    matched_fills = 0
    for entry in entries:
        cost = entry.get("cost_usd")
        # Wave 2: cost_usd is now a decimal string (root CLAUDE.md "no floats for money"); still
        # accept a bare number for any pre-wave-2 fixture/ledger line that hasn't been
        # regenerated. A malformed value must not corrupt the running total silently.
        if isinstance(cost, (int, float, str)):
            try:
                ai_cost += Decimal(str(cost))
            except (ArithmeticError, ValueError):
                pass
        if _is_accepted_trade(entry):
            accepted_orders += 1
        fill = resolve_fill(entry)
        if fill is not None:
            side, qty, price = fill
            pos = apply_fill(pos, side, qty, price, fee_bps)
            matched_fills += 1
        position_curve.append(pos)
        peak_abs_qty = max(peak_abs_qty, abs(pos.qty))

    first_mid = Decimal(str(entries[0]["request"]["mid"])) if entries else Decimal(0)
    bh_pos: Position | None = None
    if peak_abs_qty > 0 and first_mid > 0:
        bh_pos = apply_fill(Position(), "buy", peak_abs_qty, first_mid, fee_bps)

    curve: list[EquityPoint] = []
    for entry, agent_pos in zip(entries, position_curve, strict=True):
        request = entry.get("request", {})
        mid = Decimal(str(request.get("mid", "0")))
        ts_ns = int(request.get("ts_ns", 0))
        bh_equity = bh_pos.total_pnl_after_fees(mid) if bh_pos is not None else None
        curve.append(
            EquityPoint(_day(ts_ns), ts_ns, agent_pos.total_pnl_after_fees(mid), bh_equity)
        )
    pos = position_curve[-1] if position_curve else pos

    max_drawdown = Decimal(0)
    peak = Decimal("-Infinity")
    for point in curve:
        peak = max(peak, point.agent_equity)
        max_drawdown = max(max_drawdown, peak - point.agent_equity)

    mark_final = Decimal(str(entries[-1]["request"]["mid"])) if entries else Decimal(0)
    agent_summary = {
        "realized_pnl_usd": str(pos.realized_pnl),
        "fees_paid_usd": str(pos.fees_paid),
        "unrealized_pnl_usd": str(pos.unrealized_pnl(mark_final)),
        "total_pnl_after_fees_usd": str(pos.total_pnl_after_fees(mark_final)),
    }
    bh_summary = None
    if bh_pos is not None:
        bh_summary = {
            "realized_pnl_usd": str(bh_pos.realized_pnl),
            "fees_paid_usd": str(bh_pos.fees_paid),
            "unrealized_pnl_usd": str(bh_pos.unrealized_pnl(mark_final)),
            "total_pnl_after_fees_usd": str(bh_pos.total_pnl_after_fees(mark_final)),
        }

    result = PnLResult(
        source=SOURCE_DECISION_LEDGER,  # caller (build_report/main) overrides for --source broker
        entry_count=len(entries),
        agent=agent_summary,
        no_trade={"total_pnl_usd": "0"},
        buy_and_hold=bh_summary,
        max_drawdown_usd=str(max_drawdown),
        ai_cost_usd=str(ai_cost),
        fills={"accepted_orders": accepted_orders, "matched_fills": matched_fills},
        stop_rule={},  # filled in by evaluate_stop_rule
    )
    return result, curve


def evaluate_stop_rule(
    result: PnLResult, curve: list[EquityPoint], policy: dict[str, Any]
) -> dict[str, Any]:
    thresholds = policy["stop_rule"]
    max_dd = Decimal(str(thresholds["max_drawdown_usd"]))
    max_days = int(thresholds["max_consecutive_days_behind_buy_and_hold"])
    reasons = []

    drawdown = Decimal(result.max_drawdown_usd)
    if drawdown > max_dd:
        reasons.append(f"drawdown ${drawdown} exceeds ${max_dd}")

    if any(p.bh_equity is not None for p in curve):
        by_day: dict[str, EquityPoint] = {}
        for p in curve:
            by_day[p.day] = p  # last entry per day wins: closing snapshot
        days_sorted = sorted(by_day)
        streak = 0
        for day in reversed(days_sorted):
            point = by_day[day]
            if point.bh_equity is not None and point.agent_equity < point.bh_equity:
                streak += 1
            else:
                break
        if streak >= max_days:
            reasons.append(
                f"{streak} consecutive day(s) behind buy-and-hold (threshold {max_days})"
            )

    # Take-profit rule: stop trading for good once cumulative realized+unrealized profit
    # (after fees -- the same total the equity curve and drawdown above are built from) reaches the
    # target. Optional: `thresholds.get` so a caller's policy dict from before this rule existed
    # (e.g. this function's own tests) still parses. `daily_close.py` is what turns
    # `take_profit_reached` into a permanent halt; this function only detects it.
    take_profit_threshold = thresholds.get("take_profit_usd")
    take_profit_reached = False
    thresholds_out = {
        "max_drawdown_usd": str(max_dd),
        "max_consecutive_days_behind_buy_and_hold": max_days,
    }
    if take_profit_threshold is not None:
        take_profit = Decimal(str(take_profit_threshold))
        thresholds_out["take_profit_usd"] = str(take_profit)
        cumulative_pnl = Decimal(result.agent["total_pnl_after_fees_usd"])
        if cumulative_pnl >= take_profit:
            take_profit_reached = True
            reasons.append(
                f"cumulative profit ${cumulative_pnl} reached the ${take_profit} take-profit target"
            )

    return {
        "triggered": bool(reasons),
        "reasons": reasons,
        "take_profit_reached": take_profit_reached,
        "thresholds": thresholds_out,
        "thresholds_source": ("placeholder thresholds, see scripts/reports/pnl_policy.toml"),
    }


def load_policy(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)


def build_report(
    ledger: Path,
    policy_path: Path,
    source: str = SOURCE_DECISION_LEDGER,
    broker_journal: Path = DEFAULT_BROKER_JOURNAL,
) -> tuple[PnLResult, list[EquityPoint]]:
    entries = load_entries(ledger)
    policy = load_policy(policy_path)
    fee_bps = Decimal(str(policy["fees"]["fee_bps"]))
    if source == SOURCE_RECONCILED:
        resolver = make_broker_fill_resolver(load_broker_journal(broker_journal))
    elif source == SOURCE_DECISION_LEDGER:
        resolver = _decision_ledger_fill
    else:
        expected = f"{SOURCE_DECISION_LEDGER!r} or {SOURCE_RECONCILED!r}"
        raise ValueError(f"unknown P&L source {source!r} (expected {expected})")
    result, curve = compute(entries, fee_bps, resolver)
    result.source = source
    result.stop_rule = evaluate_stop_rule(result, curve, policy)
    return result, curve


def daily_file_path(out_dir: Path, date: str) -> Path:
    return out_dir / f"{date}.json"


def write_daily_record(result: PnLResult, date: str, out_dir: Path) -> Path:
    """Append-only: writing the same date twice with identical content is a no-op; writing
    different content for an already-recorded date is a hard error (root CLAUDE.md rule 10:
    records are append-only and never edited retroactively)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = daily_file_path(out_dir, date)
    payload = {"date": date, **asdict(result)}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        existing = path.read_text()
        if existing == text:
            return path
        raise RuntimeError(
            f"{path} already exists with different content — daily records are append-only "
            "and never edited retroactively (root CLAUDE.md rule 10); this date's file must "
            "not change once written"
        )
    path.write_text(text)
    return path


def render_markdown(result: PnLResult, date: str) -> str:
    source_note = (
        "Not yet reconciled against a venue statement (issue #45)."
        if result.source == SOURCE_DECISION_LEDGER
        else f"Fills matched against the broker journal: {result.fills['matched_fills']}/"
        f"{result.fills['accepted_orders']} accepted order(s) resolved to a real fill."
    )
    lines = [
        f"# P&L report — {date}",
        "",
        f"Source: `{result.source}` ({result.entry_count} decision(s)). {source_note}",
        "",
        "## Agent (after fees)",
        f"- realized: ${result.agent['realized_pnl_usd']}",
        f"- fees paid: ${result.agent['fees_paid_usd']}",
        f"- unrealized: ${result.agent['unrealized_pnl_usd']}",
        f"- total: ${result.agent['total_pnl_after_fees_usd']}",
        "",
        "## Baselines",
        f"- no-trade: ${result.no_trade['total_pnl_usd']}",
    ]
    if result.buy_and_hold is not None:
        lines.append(f"- buy-and-hold: ${result.buy_and_hold['total_pnl_after_fees_usd']}")
    else:
        lines.append(
            "- buy-and-hold: not computed (agent never took a position to size it against)"
        )
    lines += [
        "",
        f"## Drawdown: ${result.max_drawdown_usd}",
        f"## AI cost: ${result.ai_cost_usd}",
        "",
        "## Stop rule",
        f"- triggered: {result.stop_rule['triggered']}",
    ]
    for reason in result.stop_rule["reasons"]:
        lines.append(f"  - {reason}")
    lines.append(f"- thresholds: {result.stop_rule['thresholds_source']}")
    if result.reconciliation is not None:
        lines += [
            "",
            "## Reconciliation",
            f"- status: {result.reconciliation.get('status')}",
        ]
        detail = result.reconciliation.get("detail")
        if detail:
            lines.append(f"- detail: {detail}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--source",
        choices=[SOURCE_DECISION_LEDGER, "broker"],
        default=SOURCE_DECISION_LEDGER,
        help="'broker' computes fills from --broker-journal (the gateway's report_execution "
        "record) instead of assuming every accepted decision filled at its own price/qty",
    )
    parser.add_argument("--broker-journal", type=Path, default=DEFAULT_BROKER_JOURNAL)
    args = parser.parse_args(argv)

    entries = load_entries(args.ledger)
    date = args.date
    if date is None:
        date = (
            _day(int(entries[-1]["request"]["ts_ns"]))
            if entries
            else dt.datetime.now(dt.UTC).date().isoformat()
        )

    source = SOURCE_RECONCILED if args.source == "broker" else SOURCE_DECISION_LEDGER
    result, _curve = build_report(
        args.ledger, args.policy, source=source, broker_journal=args.broker_journal
    )
    path = write_daily_record(result, date, args.out)
    print(render_markdown(result, date))
    print(f"(written to {path})")
    return 1 if result.stop_rule["triggered"] else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
