#!/usr/bin/env python3
"""Issue #54 (+ #53's pipeline): does a panel of agents beat one, at the exact same total AI
usage?

  scripts/eval/ensemble_compare.py [--ensemble-size N] [--aggregation RULE] [options...]

## Why this exists

`llm-trading-eval-research.md` §2 gives two reasons not to assume a panel helps: Tran & Kiela
(arXiv:2604.02460) find the reported multi-agent advantage in prior trading-agent papers
(TradingAgents, FinAgent, FinCon) was largely uncontrolled compute (multi-agent setups silently
spend more total tokens), not an architectural benefit; Kim/Garg/Peng/Garg (arXiv:2506.07962) find
instances of the same base model agree ~60% of the time when both err, and larger/more-accurate
models are *more* correlated, not less -- so a vote among same-family agents is not a
variance-reduction ensemble the way independent estimators would be. ADR-0040 Challenge #4's
resolution: parallel agents ship only if they beat one agent at *equal* token budget on the forward
eval, measured honestly, never assumed.

## What "equal total AI usage" means here, exactly

This never approximates parity by running the ensemble at some fraction of a budget and hoping it
roughly matches -- it is architectural. `agent/src/cli.ts`'s ensemble wiring (issue #54) resolves
one per-decision budget (`ai_gate.py loop-agent-eval`'s `max_budget_usd`) once, then gives each of
the N live members exactly `total / N` (`agent/src/decider/ensemble.py`'s `splitBudgetEqually`,
`LiveDecider`'s `maxBudgetUsdOverride`) -- so the panel's combined worst-case spend is the same T a
single agent would get, not N times it. In fake mode there is no real spend either way (cost_usd
is never reported by FakeDecider) -- this script reports the *measured*, not assumed, cost_usd
summed from each run's own ledger, so "equal usage" is never asserted, only shown.

## What this reports

- The exact total/per-member budget numbers (live mode) or the exact measured cost (both modes),
  never an approximation.
- Per-scenario: single agent's action, ensemble's aggregated action, and -- separately from the
  aggregate score -- the ensemble's own internal agreement fraction (how many of the N members'
  individual votes matched each other), which is what "did the panel's members fail together"
  means operationally: a high agreement fraction under DIFFERENT member configs is evidence of
  real (if partial) diversity; under IDENTICAL member configs (this script's default) it is
  agreement by construction, not evidence of anything, and this script says so.
- A paired bootstrap test (net P&L after costs, ensemble vs single, reusing
  `scripts/eval/promote.py`'s own `bootstrap_mean_diff` -- one implementation, not a second copy)
  and a plain ADOPT / DO_NOT_ADOPT verdict with the exact margin -- never left open-ended (issue
  #54's own acceptance criterion).
- Whether single and ensemble share the same base `model` (in fake mode this is always true:
  `fake-rule-v1` either way, since only the rule threshold differs), with the same
  correlated-errors caution `promote.py` already prints, reused verbatim rather than re-worded.

Every run appends one record to `evals/promotions/ensemble_comparisons.jsonl` (root CLAUDE.md rule
6: unrecorded experiments don't exist), via `research/registry`'s generic append store.

Standard library only, matching every other script in scripts/eval and scripts/reports.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = ROOT / "evals/promotions"
DEFAULT_POLICY = ROOT / "evals/promotions/policy.toml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts/reports"))
sys.path.insert(0, str(ROOT / "research"))
import live_agent  # noqa: E402
import pnl_report  # noqa: E402
import promote  # noqa: E402  -- reuses bootstrap_mean_diff, read_champion_defaults, gate_allows_live

from registry import record as registry_record  # noqa: E402

ADOPT, DO_NOT_ADOPT = "ADOPT", "DO_NOT_ADOPT"
AGGREGATION_RULES = ("unanimous", "majority", "confidence_weighted")


@dataclass
class ScenarioComparison:
    scenario: str
    single_trade_count: int
    single_pnl_after_costs_usd: str
    single_cost_usd: str
    ensemble_trade_count: int
    ensemble_pnl_after_costs_usd: str
    ensemble_cost_usd: str
    ensemble_decision_count: int
    ensemble_mean_agreement_fraction: float
    ensemble_min_agreement_fraction: float
    ensemble_unanimous_fraction: float
    per_decision_agreement_fractions: list[float]


def _sum_cost_usd(entries: list[dict[str, Any]]) -> Decimal:
    total = Decimal(0)
    for e in entries:
        cost = e.get("cost_usd")
        if cost is not None:
            total += Decimal(cost)
    return total


def _pnl_after_costs(entries: list[dict[str, Any]]) -> Decimal:
    result, _curve = pnl_report.compute(entries, fee_bps=Decimal("0"))
    return Decimal(result.agent["total_pnl_after_fees_usd"])


def _trade_count(entries: list[dict[str, Any]]) -> int:
    return sum(1 for e in entries if e["decision"]["action"] != "no_trade")


def _member_actions_per_decision(entries: list[dict[str, Any]]) -> list[list[str]]:
    """Every decision's per-member votes across the whole scenario, from the ledger's
    `raw.members` (issue #54: "per-member decisions recorded in the ledger raw") -- not just the
    scenario's last decision, so a panel that only disagrees mid-scenario is still counted."""
    out: list[list[str]] = []
    for e in entries:
        raw = e.get("raw") or {}
        members = raw.get("members") if isinstance(raw, dict) else None
        if not isinstance(members, list):
            raise RuntimeError(
                f"expected an ensemble ledger entry (raw.members) but got {e.get('raw')!r} -- "
                "QC_ENSEMBLE_SIZE/QC_ENSEMBLE_AGGREGATION did not take effect"
            )
        out.append([str(m["decision"]["action"]) for m in members])
    return out


def _agreement_fraction(member_actions: list[str]) -> float:
    """Fraction of members whose action matches the plurality action among them, for one
    decision -- 1.0 means every member voted the same way on that decision (perfect agreement;
    under identical member configs this is agreement by construction, not evidence of diversity
    or its absence)."""
    if not member_actions:
        return 1.0
    counts: dict[str, int] = {}
    for a in member_actions:
        counts[a] = counts.get(a, 0) + 1
    return max(counts.values()) / len(member_actions)


def run_scenario_comparison(
    scenario: Path,
    root: Path,
    mode: str,
    single_threshold: float,
    ensemble_size: int,
    aggregation: str,
    ensemble_thresholds: list[float] | None,
    prompt_version: str | None,
) -> ScenarioComparison:
    single_env = (
        {"QC_PROMPT_VERSION": prompt_version} if mode == "live" and prompt_version else {}
    ) | ({} if mode == "live" else {"QC_FAKE_PROB_THRESHOLD": str(single_threshold)})
    single_ledger = live_agent._run_agent_once(scenario, root, mode=mode, extra_env=single_env)
    single_entries = pnl_report.load_entries(single_ledger)

    ensemble_env: dict[str, str] = {
        "QC_ENSEMBLE_SIZE": str(ensemble_size),
        "QC_ENSEMBLE_AGGREGATION": aggregation,
    }
    if mode == "live" and prompt_version:
        ensemble_env["QC_PROMPT_VERSION"] = prompt_version
    if mode != "live":
        # Explicit either way (never left to FakeDecider's own internal default): when the caller
        # didn't ask for distinct member thresholds, every member gets exactly `single_threshold`
        # -- the same value the single arm uses -- not whatever fake.ts's own default happens to
        # be, so "identical members" is actually true even when --single-prob-threshold overrides
        # the committed default.
        effective_thresholds = (
            ensemble_thresholds
            if ensemble_thresholds is not None
            else [single_threshold] * ensemble_size
        )
        ensemble_env["QC_ENSEMBLE_FAKE_PROB_THRESHOLDS"] = ",".join(
            str(t) for t in effective_thresholds
        )
    ensemble_ledger = live_agent._run_agent_once(scenario, root, mode=mode, extra_env=ensemble_env)
    ensemble_entries = pnl_report.load_entries(ensemble_ledger)

    per_decision_actions = _member_actions_per_decision(ensemble_entries)
    agreement_fractions = [_agreement_fraction(a) for a in per_decision_actions]
    return ScenarioComparison(
        scenario=scenario.name,
        single_trade_count=_trade_count(single_entries),
        single_pnl_after_costs_usd=str(_pnl_after_costs(single_entries)),
        single_cost_usd=str(_sum_cost_usd(single_entries)),
        ensemble_trade_count=_trade_count(ensemble_entries),
        ensemble_pnl_after_costs_usd=str(_pnl_after_costs(ensemble_entries)),
        ensemble_cost_usd=str(_sum_cost_usd(ensemble_entries)),
        ensemble_decision_count=len(agreement_fractions),
        ensemble_mean_agreement_fraction=(sum(agreement_fractions) / len(agreement_fractions))
        if agreement_fractions
        else 1.0,
        ensemble_min_agreement_fraction=min(agreement_fractions) if agreement_fractions else 1.0,
        ensemble_unanimous_fraction=(
            sum(1 for f in agreement_fractions if f == 1.0) / len(agreement_fractions)
            if agreement_fractions
            else 1.0
        ),
        per_decision_agreement_fractions=agreement_fractions,
    )


@dataclass
class ComparisonRecord:
    ts: str
    mode: str
    ensemble_size: int
    aggregation: str
    single_threshold: float
    ensemble_thresholds: list[float] | None
    total_budget_usd: str | None
    per_member_budget_usd: str | None
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    single_total_pnl_usd: str = "0"
    ensemble_total_pnl_usd: str = "0"
    single_total_cost_usd: str = "0"
    ensemble_total_cost_usd: str = "0"
    mean_agreement_fraction: float = 1.0
    total_decisions: int = 0
    observed_diff_usd: float = 0.0
    ci_lo: float = 0.0
    ci_hi: float = 0.0
    p_value: float = 1.0
    verdict: str = DO_NOT_ADOPT
    same_base_model: bool = True
    correlated_errors_note: str = ""


def compare(
    root: Path,
    scenarios: list[Path],
    mode: str,
    ensemble_size: int,
    aggregation: str,
    single_threshold: float,
    ensemble_thresholds: list[float] | None,
    prompt_version: str | None,
    alpha: float,
    min_improvement_usd: Decimal,
    n_bootstrap: int,
    seed: int,
) -> ComparisonRecord:
    total_budget_usd: str | None = None
    per_member_budget_usd: str | None = None
    if mode == "live":
        # Read-only: does not itself run a decision, just reports the exact numbers the real
        # cli.ts run will also derive (same ai_gate.py answer, same division).
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(root / "scripts/ci/ai_gate.py"),
                "loop-agent-eval",
                "--root",
                str(root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            for line in (proc.stdout + proc.stderr).splitlines():
                if line.startswith("max_budget_usd="):
                    total = float(line.split("=", 1)[1])
                    total_budget_usd = str(total)
                    per_member_budget_usd = str(total / ensemble_size)

    comparisons = [
        run_scenario_comparison(
            s,
            root,
            mode,
            single_threshold,
            ensemble_size,
            aggregation,
            ensemble_thresholds,
            prompt_version,
        )
        for s in scenarios
    ]

    single_pnls = [float(c.single_pnl_after_costs_usd) for c in comparisons]
    ensemble_pnls = [float(c.ensemble_pnl_after_costs_usd) for c in comparisons]
    rng = random.Random(seed)  # noqa: S311 - deterministic resampling, not a crypto use
    observed, lo, hi, p_improve, _p_worsen = promote.bootstrap_mean_diff(
        single_pnls, ensemble_pnls, paired=True, n_boot=n_bootstrap, rng=rng
    )
    adopt = Decimal(str(observed)) >= min_improvement_usd and p_improve <= alpha
    # Averaged over every individual decision across every scenario (not an average of each
    # scenario's own average), so a long scenario doesn't get diluted to the same weight as a
    # short one.
    all_fractions = [f for c in comparisons for f in c.per_decision_agreement_fractions]
    mean_agreement = sum(all_fractions) / len(all_fractions) if all_fractions else 1.0
    total_decisions = len(all_fractions)

    single_model = "fake-rule-v1" if mode != "live" else "live"
    ensemble_model = (
        single_model  # ensemble members run the same base decider kind as the single agent
    )
    same_base_model = single_model == ensemble_model

    return ComparisonRecord(
        ts=dt.datetime.now(dt.UTC).isoformat(),
        mode=mode,
        ensemble_size=ensemble_size,
        aggregation=aggregation,
        single_threshold=single_threshold,
        ensemble_thresholds=ensemble_thresholds,
        total_budget_usd=total_budget_usd,
        per_member_budget_usd=per_member_budget_usd,
        scenarios=[asdict(c) for c in comparisons],
        single_total_pnl_usd=str(sum(Decimal(c.single_pnl_after_costs_usd) for c in comparisons)),
        ensemble_total_pnl_usd=str(
            sum(Decimal(c.ensemble_pnl_after_costs_usd) for c in comparisons)
        ),
        single_total_cost_usd=str(sum(Decimal(c.single_cost_usd) for c in comparisons)),
        ensemble_total_cost_usd=str(sum(Decimal(c.ensemble_cost_usd) for c in comparisons)),
        mean_agreement_fraction=mean_agreement,
        total_decisions=total_decisions,
        observed_diff_usd=observed,
        ci_lo=lo,
        ci_hi=hi,
        p_value=p_improve,
        verdict=ADOPT if adopt else DO_NOT_ADOPT,
        same_base_model=same_base_model,
        correlated_errors_note=(
            promote.CORRELATED_ERRORS_NOTE.format(model=single_model) if same_base_model else ""
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--ensemble-size", type=int, default=3)
    parser.add_argument("--aggregation", choices=AGGREGATION_RULES, default="majority")
    parser.add_argument("--single-prob-threshold", type=float, default=None)
    parser.add_argument(
        "--ensemble-prob-thresholds",
        default=None,
        help="Comma list, one per member, fake mode only. Default: every member gets the SAME "
        "threshold as the single agent -- the fair, worst-case-for-the-ensemble baseline (members "
        "are then identical, so any agreement is by construction, not diversity).",
    )
    parser.add_argument(
        "--prompt-version",
        default=None,
        help="Live mode only: pins both arms to the same prompt version.",
    )
    parser.add_argument("--scenarios-dir", type=Path, default=None)
    parser.add_argument("--extra-scenarios-dir", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)

    root: Path = args.root
    defaults = promote.read_champion_defaults(root)
    single_threshold = (
        args.single_prob_threshold
        if args.single_prob_threshold is not None
        else defaults.prob_threshold
    )

    ensemble_thresholds: list[float] | None = None
    if args.ensemble_prob_thresholds is not None:
        ensemble_thresholds = [float(t) for t in args.ensemble_prob_thresholds.split(",")]
        if len(ensemble_thresholds) != args.ensemble_size:
            print(
                f"--ensemble-prob-thresholds has {len(ensemble_thresholds)} entries, "
                f"expected --ensemble-size {args.ensemble_size}",
                file=sys.stderr,
            )
            return 2

    live = args.live
    if live:
        allowed, reason = promote.gate_allows_live(root)
        if not allowed:
            print(
                f"--live refused: {reason} (falling back to fake mode would misreport a live "
                "result as gated; exiting instead)",
                file=sys.stderr,
            )
            return 1
    mode = "live" if live else "fake"

    policy_path = args.policy or DEFAULT_POLICY
    policy = promote.load_policy(policy_path)

    scenarios_dir = args.scenarios_dir or (root / "evals/scenarios")
    extra_scenarios_dir = args.extra_scenarios_dir or (root / "evals/scenarios/from_ledger")
    scenarios = promote.scenario_files(scenarios_dir, extra_scenarios_dir)
    if not scenarios:
        print("no scenario files found under evals/scenarios/", file=sys.stderr)
        return 2

    record = compare(
        root,
        scenarios,
        mode,
        args.ensemble_size,
        args.aggregation,
        single_threshold,
        ensemble_thresholds,
        args.prompt_version,
        alpha=float(policy["alpha"]),
        min_improvement_usd=Decimal(str(policy["min_improvement_usd"])),
        n_bootstrap=int(policy["n_bootstrap"]),
        seed=int(policy["seed"]),
    )

    out_dir = args.out_dir or DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    registry_record(out_dir / "ensemble_comparisons.jsonl", record)

    print(
        f"mode={record.mode} ensemble_size={record.ensemble_size} aggregation={record.aggregation}"
    )
    if record.total_budget_usd is not None:
        print(
            f"total budget per decision: ${record.total_budget_usd} "
            f"(single agent gets all of it; each of {record.ensemble_size} ensemble members gets "
            f"${record.per_member_budget_usd} -- exactly equal total usage, by construction)"
        )
    print(
        f"measured cost: single=${record.single_total_cost_usd} "
        f"ensemble=${record.ensemble_total_cost_usd} "
        "(actual, summed from each run's own ledger -- not assumed)"
    )
    print(
        f"net P&L after costs: single=${record.single_total_pnl_usd} "
        f"ensemble=${record.ensemble_total_pnl_usd}"
    )
    print(
        f"paired diff (ensemble - single): ${record.observed_diff_usd:.4f} "
        f"[{record.ci_lo:.4f}, {record.ci_hi:.4f}] p={record.p_value:.4f}"
    )
    print(
        f"ensemble internal agreement (fraction of members voting the plurality action per "
        f"decision, averaged over {record.total_decisions} decisions across {len(scenarios)} "
        f"scenarios): {record.mean_agreement_fraction:.3f}"
    )
    worst_scenarios = [s for s in record.scenarios if s["ensemble_min_agreement_fraction"] < 1.0]
    if worst_scenarios:
        print(
            f"scenarios where the panel split on at least one decision: "
            f"{', '.join(s['scenario'] for s in worst_scenarios)}"
        )
    else:
        print("no scenario had even one split decision -- the panel agreed on every decision")
    if record.ensemble_thresholds is None and mode != "live":
        print(
            "note: every ensemble member used the SAME rule threshold as the single agent -- "
            "any agreement above is by construction, not evidence of real diversity"
        )
    if record.same_base_model:
        print(f"note: {record.correlated_errors_note}")
    print(f"verdict: {record.verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
