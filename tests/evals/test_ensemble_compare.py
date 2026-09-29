"""scripts/eval/ensemble_compare.py (issue #54): the pure per-decision agreement math, the
"identical members by default" threshold-filling behavior, and the compare() pipeline end to end
with the subprocess-spawning scenario runner monkeypatched — same split as
tests/evals/test_promote.py (subprocess-spawning functions are exercised operationally via `just
ensemble-compare`, not spawned in this fast, no-subprocess suite)."""

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "ensemble_compare", ROOT / "scripts/eval/ensemble_compare.py"
)
assert SPEC and SPEC.loader
ensemble_compare = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ensemble_compare
SPEC.loader.exec_module(ensemble_compare)


def _default_policy() -> dict[str, object]:
    return {"alpha": 0.05, "min_improvement_usd": "0.01", "n_bootstrap": 200, "seed": 0}


def _ledger_entry(
    action: str, raw_members: list[str] | None = None, cost_usd: str | None = None
) -> dict:
    entry = {"decision": {"action": action}}
    if raw_members is not None:
        entry["raw"] = {
            "aggregation": "majority",
            "members": [{"decision": {"action": a}} for a in raw_members],
        }
    if cost_usd is not None:
        entry["cost_usd"] = cost_usd
    return entry


# --- pure helpers ------------------------------------------------------------------------------


def test_agreement_fraction_is_one_when_every_member_agrees() -> None:
    assert ensemble_compare._agreement_fraction(["buy", "buy", "buy"]) == 1.0


def test_agreement_fraction_is_the_plurality_share_on_a_split_vote() -> None:
    assert ensemble_compare._agreement_fraction(["buy", "buy", "sell"]) == 2 / 3


def test_agreement_fraction_of_empty_list_is_one_not_zero() -> None:
    # An empty vote is not a disagreement -- there is nothing to disagree about.
    assert ensemble_compare._agreement_fraction([]) == 1.0


def test_trade_count_excludes_no_trade() -> None:
    entries = [_ledger_entry("buy"), _ledger_entry("no_trade"), _ledger_entry("sell")]
    assert ensemble_compare._trade_count(entries) == 2


def test_sum_cost_usd_treats_missing_cost_as_zero() -> None:
    entries = [_ledger_entry("buy", cost_usd="0.5"), _ledger_entry("no_trade")]
    assert ensemble_compare._sum_cost_usd(entries) == Decimal("0.5")


def test_member_actions_per_decision_reads_every_entry_not_just_the_last() -> None:
    entries = [
        _ledger_entry("no_trade", raw_members=["no_trade", "no_trade", "no_trade"]),
        _ledger_entry("buy", raw_members=["buy", "buy", "sell"]),
    ]
    result = ensemble_compare._member_actions_per_decision(entries)
    assert result == [["no_trade", "no_trade", "no_trade"], ["buy", "buy", "sell"]]


def test_member_actions_per_decision_fails_closed_when_raw_members_is_missing() -> None:
    entries = [_ledger_entry("buy")]  # no raw.members at all
    try:
        ensemble_compare._member_actions_per_decision(entries)
        raise AssertionError("expected a RuntimeError")
    except RuntimeError as e:
        assert "QC_ENSEMBLE_SIZE" in str(e)


# --- compare(): the pipeline, with the subprocess-spawning scenario runner monkeypatched -------


def test_compare_reports_adopt_only_on_a_clear_consistent_improvement(
    tmp_path, monkeypatch
) -> None:
    scenarios = [tmp_path / "worldline-01.csv", tmp_path / "worldline-02.csv"]
    for s in scenarios:
        s.write_text("# fixture, not a real recording\n")

    def fake_scenario_comparison(
        scenario,
        root,
        mode,
        single_threshold,
        ensemble_size,
        aggregation,
        ensemble_thresholds,
        prompt_version,
    ):
        return ensemble_compare.ScenarioComparison(
            scenario=scenario.name,
            single_trade_count=1,
            single_pnl_after_costs_usd="0",
            single_cost_usd="0",
            ensemble_trade_count=1,
            ensemble_pnl_after_costs_usd="5",  # a clear, consistent $5 improvement every scenario
            ensemble_cost_usd="0",
            ensemble_decision_count=1,
            ensemble_mean_agreement_fraction=1.0,
            ensemble_min_agreement_fraction=1.0,
            ensemble_unanimous_fraction=1.0,
            per_decision_agreement_fractions=[1.0],
        )

    monkeypatch.setattr(ensemble_compare, "run_scenario_comparison", fake_scenario_comparison)
    record = ensemble_compare.compare(
        ROOT,
        scenarios,
        "fake",
        ensemble_size=3,
        aggregation="majority",
        single_threshold=0.6,
        ensemble_thresholds=None,
        prompt_version=None,
        alpha=0.05,
        min_improvement_usd=Decimal("0.01"),
        n_bootstrap=200,
        seed=0,
    )
    assert record.verdict == ensemble_compare.ADOPT
    assert record.observed_diff_usd == 5.0


def test_compare_reports_do_not_adopt_when_there_is_no_improvement(tmp_path, monkeypatch) -> None:
    scenarios = [tmp_path / "worldline-01.csv"]
    scenarios[0].write_text("# fixture\n")

    def fake_scenario_comparison(
        scenario,
        root,
        mode,
        single_threshold,
        ensemble_size,
        aggregation,
        ensemble_thresholds,
        prompt_version,
    ):
        return ensemble_compare.ScenarioComparison(
            scenario=scenario.name,
            single_trade_count=0,
            single_pnl_after_costs_usd="0",
            single_cost_usd="0",
            ensemble_trade_count=0,
            ensemble_pnl_after_costs_usd="0",
            ensemble_cost_usd="0",
            ensemble_decision_count=2,
            ensemble_mean_agreement_fraction=1.0,
            ensemble_min_agreement_fraction=1.0,
            ensemble_unanimous_fraction=1.0,
            per_decision_agreement_fractions=[1.0, 1.0],
        )

    monkeypatch.setattr(ensemble_compare, "run_scenario_comparison", fake_scenario_comparison)
    record = ensemble_compare.compare(
        ROOT,
        scenarios,
        "fake",
        ensemble_size=3,
        aggregation="majority",
        single_threshold=0.6,
        ensemble_thresholds=None,
        prompt_version=None,
        alpha=0.05,
        min_improvement_usd=Decimal("0.01"),
        n_bootstrap=200,
        seed=0,
    )
    assert record.verdict == ensemble_compare.DO_NOT_ADOPT
    assert record.mean_agreement_fraction == 1.0
    assert record.total_decisions == 2  # summed across the one scenario's two decisions


def test_compare_weights_agreement_by_decision_not_by_scenario(tmp_path, monkeypatch) -> None:
    """A scenario with many decisions should not be diluted to the same weight as a scenario with
    one -- mean_agreement_fraction is the mean over every individual decision, not the mean of
    each scenario's own mean."""
    scenarios = [tmp_path / "worldline-01.csv", tmp_path / "worldline-02.csv"]
    for s in scenarios:
        s.write_text("# fixture\n")

    def fake_scenario_comparison(
        scenario,
        root,
        mode,
        single_threshold,
        ensemble_size,
        aggregation,
        ensemble_thresholds,
        prompt_version,
    ):
        # worldline-01: one decision, perfect agreement. worldline-02: three decisions, all split.
        if scenario.name == "worldline-01.csv":
            fractions = [1.0]
        else:
            fractions = [0.0, 0.0, 0.0]
        return ensemble_compare.ScenarioComparison(
            scenario=scenario.name,
            single_trade_count=0,
            single_pnl_after_costs_usd="0",
            single_cost_usd="0",
            ensemble_trade_count=0,
            ensemble_pnl_after_costs_usd="0",
            ensemble_cost_usd="0",
            ensemble_decision_count=len(fractions),
            ensemble_mean_agreement_fraction=sum(fractions) / len(fractions),
            ensemble_min_agreement_fraction=min(fractions),
            ensemble_unanimous_fraction=sum(1 for f in fractions if f == 1.0) / len(fractions),
            per_decision_agreement_fractions=fractions,
        )

    monkeypatch.setattr(ensemble_compare, "run_scenario_comparison", fake_scenario_comparison)
    record = ensemble_compare.compare(
        ROOT,
        scenarios,
        "fake",
        ensemble_size=3,
        aggregation="majority",
        single_threshold=0.6,
        ensemble_thresholds=None,
        prompt_version=None,
        alpha=0.05,
        min_improvement_usd=Decimal("0.01"),
        n_bootstrap=200,
        seed=0,
    )
    # 4 decisions total: one at 1.0, three at 0.0 -> mean 0.25, not (1.0 + 0.0) / 2 = 0.5.
    assert record.mean_agreement_fraction == 0.25
    assert record.total_decisions == 4


def test_compare_correlated_errors_note_present_in_fake_mode(tmp_path, monkeypatch) -> None:
    scenarios = [tmp_path / "worldline-01.csv"]
    scenarios[0].write_text("# fixture\n")

    def fake_scenario_comparison(
        scenario,
        root,
        mode,
        single_threshold,
        ensemble_size,
        aggregation,
        ensemble_thresholds,
        prompt_version,
    ):
        return ensemble_compare.ScenarioComparison(
            scenario=scenario.name,
            single_trade_count=0,
            single_pnl_after_costs_usd="0",
            single_cost_usd="0",
            ensemble_trade_count=0,
            ensemble_pnl_after_costs_usd="0",
            ensemble_cost_usd="0",
            ensemble_decision_count=1,
            ensemble_mean_agreement_fraction=1.0,
            ensemble_min_agreement_fraction=1.0,
            ensemble_unanimous_fraction=1.0,
            per_decision_agreement_fractions=[1.0],
        )

    monkeypatch.setattr(ensemble_compare, "run_scenario_comparison", fake_scenario_comparison)
    record = ensemble_compare.compare(
        ROOT,
        scenarios,
        "fake",
        ensemble_size=3,
        aggregation="majority",
        single_threshold=0.6,
        ensemble_thresholds=None,
        prompt_version=None,
        alpha=0.05,
        min_improvement_usd=Decimal("0.01"),
        n_bootstrap=200,
        seed=0,
    )
    # Fake mode: single and ensemble members are both the deterministic rule decider
    # ("fake-rule-v1" either way) -- always the same base model, so the caution always applies.
    assert record.same_base_model is True
    assert "correlated" in record.correlated_errors_note.lower()


# --- main(): CLI-level fail-closed validation (no subprocess needed for these two paths) -------


def test_main_refuses_a_thresholds_list_whose_length_doesnt_match_ensemble_size(tmp_path) -> None:
    exit_code = ensemble_compare.main(
        ["--root", str(ROOT), "--ensemble-size", "3", "--ensemble-prob-thresholds", "0.5,0.6"]
    )
    assert exit_code == 2


def test_main_refuses_live_when_the_gate_denies(monkeypatch) -> None:
    monkeypatch.setattr(
        ensemble_compare.promote, "gate_allows_live", lambda root: (False, "policy disabled")
    )
    exit_code = ensemble_compare.main(["--root", str(ROOT), "--live"])
    assert exit_code == 1
