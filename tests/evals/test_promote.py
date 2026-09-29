"""scripts/eval/promote.py (issue #53): the statistics (bootstrap, Holm), the two-tier
promotion decision — including the required "frozen helps but forward can't run => not
promoted" case — the correlated-errors note, and the hard "never mutates the live prompt or
decider defaults" invariant. Subprocess-spawning functions (run_candidate_frozen/forward calling
the real agent CLI) are exercised operationally via `just promote`, not spawned in this fast,
no-subprocess suite — same split as tests/evals/test_live_agent.py."""

import importlib.util
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("promote", ROOT / "scripts/eval/promote.py")
assert SPEC and SPEC.loader
promote = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promote
SPEC.loader.exec_module(promote)


# --- bootstrap / Holm -------------------------------------------------------------------------


def test_paired_bootstrap_observed_diff_is_the_mean_of_paired_differences() -> None:
    champion = [1.0, 2.0, 3.0, 4.0]
    challenger = [2.0, 3.0, 5.0, 4.0]  # diffs: 1, 1, 2, 0 -> mean 1.0
    observed, lo, hi, _p_improve, _p_worsen = promote.bootstrap_mean_diff(
        champion,
        challenger,
        paired=True,
        n_boot=2000,
        rng=random.Random(0),  # noqa: S311
    )
    assert observed == 1.0
    assert lo <= observed <= hi


def test_paired_bootstrap_p_value_is_small_for_a_clear_consistent_improvement() -> None:
    champion = [1.0, 1.0, 1.0, 1.0, 1.0]
    challenger = [2.0, 2.0, 2.0, 2.0, 2.0]  # every scenario improved by exactly 1.0
    _observed, _lo, _hi, p_improve, p_worsen = promote.bootstrap_mean_diff(
        champion,
        challenger,
        paired=True,
        n_boot=2000,
        rng=random.Random(0),  # noqa: S311
    )
    assert p_improve == 0.0  # every bootstrap mean diff is exactly 1.0: never <= 0
    assert p_worsen == 1.0  # and always >= 0: no support at all for "worse", as expected


def test_paired_bootstrap_p_values_are_both_large_when_champion_and_challenger_are_identical() -> (
    None
):
    # Degenerate, zero-variance case: every paired diff is exactly 0, so every bootstrap
    # resample's mean is also exactly 0 -- neither "> 0" nor "< 0" has any support. A naive
    # `p_worsen = 1 - p_improve` would wrongly call this significant worsening; the real,
    # independently-computed p_worsen must also be large here (module docstring's own point).
    values = [1.0, 2.0, 3.0, 4.0]
    _observed, _lo, _hi, p_improve, p_worsen = promote.bootstrap_mean_diff(
        values,
        values,
        paired=True,
        n_boot=2000,
        rng=random.Random(0),  # noqa: S311
    )
    assert p_improve == 1.0
    assert p_worsen == 1.0


def test_unpaired_bootstrap_handles_different_length_samples() -> None:
    champion = [1.0, 1.0, 1.0]
    challenger = [3.0, 3.0, 3.0, 3.0, 3.0]
    observed, _lo, _hi, p_improve, p_worsen = promote.bootstrap_mean_diff(
        champion,
        challenger,
        paired=False,
        n_boot=2000,
        rng=random.Random(0),  # noqa: S311
    )
    assert observed == 2.0
    assert p_improve == 0.0
    assert p_worsen == 1.0


def test_paired_bootstrap_rejects_mismatched_lengths() -> None:
    rng = random.Random(0)  # noqa: S311
    try:
        promote.bootstrap_mean_diff([1.0, 2.0], [1.0], paired=True, n_boot=10, rng=rng)
        raise AssertionError("expected a ValueError for mismatched paired lengths")
    except ValueError:
        pass


def test_holm_reject_matches_hand_computed_example() -> None:
    # alpha=0.05, 4 tests, already sorted ascending: p = [0.005, 0.01, 0.015, 0.20].
    # rank0 thr=0.05/4=.0125: .005 <= .0125 reject.
    # rank1 thr=0.05/3=.01667: .01 <= .01667 reject.
    # rank2 thr=0.05/2=.025: .015 <= .025 reject.
    # rank3 thr=0.05/1=.05: .20 <= .05? no -> stop.
    p_values = [0.005, 0.01, 0.015, 0.20]
    rejected = promote.holm_reject(p_values, alpha=0.05)
    assert rejected == [True, True, True, False]


def test_holm_reject_none_significant_when_all_p_values_are_large() -> None:
    assert promote.holm_reject([0.5, 0.6, 0.7], alpha=0.05) == [False, False, False]


def test_holm_reject_all_significant_when_all_p_values_are_tiny() -> None:
    assert promote.holm_reject([0.0001, 0.0002, 0.0003], alpha=0.05) == [True, True, True]


# --- decision_is_valid, against the real committed schema (never a retyped copy) ---------------


def _schema():
    return promote.load_decision_schema(ROOT)


def test_decision_is_valid_accepts_a_well_formed_no_trade() -> None:
    schema = _schema()
    decision = {"request_id": "r1", "action": "no_trade", "rationale": "no signal"}
    assert promote.decision_is_valid(decision, schema) is True


def test_decision_is_valid_accepts_a_well_formed_buy() -> None:
    schema = _schema()
    decision = {
        "request_id": "r1",
        "action": "buy",
        "qty": "1.5",
        "limit_price": "100.25",
        "rationale": "signal up",
    }
    assert promote.decision_is_valid(decision, schema) is True


def test_decision_is_valid_rejects_a_buy_missing_qty() -> None:
    schema = _schema()
    decision = {"request_id": "r1", "action": "buy", "limit_price": "100.25", "rationale": "x"}
    assert promote.decision_is_valid(decision, schema) is False


def test_decision_is_valid_rejects_a_non_decimal_qty() -> None:
    schema = _schema()
    decision = {
        "request_id": "r1",
        "action": "buy",
        "qty": "not-a-number",
        "limit_price": "100.25",
        "rationale": "x",
    }
    assert promote.decision_is_valid(decision, schema) is False


def test_decision_is_valid_rejects_an_unknown_action() -> None:
    schema = _schema()
    decision = {"request_id": "r1", "action": "hold", "rationale": "x"}
    assert promote.decision_is_valid(decision, schema) is False


def test_decision_is_valid_rejects_rationale_over_max_length() -> None:
    schema = _schema()
    decision = {"request_id": "r1", "action": "no_trade", "rationale": "x" * 501}
    assert promote.decision_is_valid(decision, schema) is False


# --- read_champion_defaults, against the real committed source (never hardcoded here) ----------


def test_read_champion_defaults_matches_committed_source() -> None:
    defaults = promote.read_champion_defaults(ROOT)
    fake_ts = (ROOT / "agent/src/decider/fake.ts").read_text()
    live_ts = (ROOT / "agent/src/decider/live.ts").read_text()
    assert f"DEFAULT_PROB_THRESHOLD = {defaults.prob_threshold}" in fake_ts
    assert f'PROMPT_VERSION = "{defaults.prompt_version}"' in live_ts


# --- evaluate_tier / decide_promotion, on canned metrics (no subprocess) ------------------------


def _metrics(
    pnl: list[float], invalid: list[float], rejects: list[float], model: str = "fake-rule-v1"
):
    return promote.ScenarioMetrics(
        net_pnl_after_costs_usd=pnl, invalid_decisions=invalid, risk_rejects=rejects, model=model
    )


def _default_policy() -> dict:
    return {"alpha": 0.05, "min_improvement_usd": "0.01", "n_bootstrap": 2000, "seed": 0}


def test_evaluate_tier_passes_on_a_clear_consistent_improvement() -> None:
    champion = _metrics([0.0] * 6, [0.0] * 6, [0.0] * 6)
    challenger = _metrics([1.0] * 6, [0.0] * 6, [0.0] * 6)
    from decimal import Decimal

    tier = promote.evaluate_tier(
        "frozen", champion, challenger, True, Decimal("0.01"), 0.05, 2000, 0
    )
    assert tier.verdict == promote.PASS


def test_evaluate_tier_fails_when_improvement_does_not_clear_the_minimum() -> None:
    from decimal import Decimal

    champion = _metrics([0.0] * 6, [0.0] * 6, [0.0] * 6)
    challenger = _metrics([0.001] * 6, [0.0] * 6, [0.0] * 6)  # tiny, below min_improvement_usd
    tier = promote.evaluate_tier(
        "frozen", champion, challenger, True, Decimal("0.01"), 0.05, 2000, 0
    )
    assert tier.verdict == promote.FAIL


def test_evaluate_tier_fails_when_challenger_significantly_increases_invalid_decisions() -> None:
    from decimal import Decimal

    champion = _metrics([0.0] * 8, [0.0] * 8, [0.0] * 8)
    challenger = _metrics([5.0] * 8, [1.0] * 8, [0.0] * 8)  # great P&L, but always invalid now
    tier = promote.evaluate_tier(
        "frozen", champion, challenger, True, Decimal("0.01"), 0.05, 2000, 0
    )
    assert tier.verdict == promote.FAIL
    assert any("invalid_decisions" in r for r in tier.reasons)
    # risk_rejects never changed (0 for both): must not be flagged as worsened too, matching the
    # fixed p_worsen semantics (see the degenerate-case bootstrap test above).
    assert not any("risk_rejects" in r for r in tier.reasons)


def test_evaluate_tier_is_unavailable_when_either_candidate_is_missing() -> None:
    from decimal import Decimal

    tier = promote.evaluate_tier(
        "forward", None, _metrics([1.0], [0.0], [0.0]), False, Decimal("0"), 0.05, 100, 0
    )
    assert tier.verdict == promote.UNAVAILABLE


def test_decide_promotion_requires_both_tiers_to_pass() -> None:
    pass_tier = promote.TierResult("t", promote.PASS, [], {})
    fail_tier = promote.TierResult("t", promote.FAIL, ["bad"], {})

    assert promote.decide_promotion(pass_tier, pass_tier)[0] == promote.PROMOTED
    assert promote.decide_promotion(pass_tier, fail_tier)[0] == promote.NOT_PROMOTED
    assert promote.decide_promotion(fail_tier, pass_tier)[0] == promote.NOT_PROMOTED


def test_frozen_pass_forward_unavailable_is_not_promoted() -> None:
    """Issue #53's own acceptance criterion, proven directly: a challenger that helps on the
    frozen suite but has no forward/shadow evidence yet must NOT be promoted."""
    pass_tier = promote.TierResult("frozen", promote.PASS, [], {})
    unavailable_tier = promote.TierResult(
        "forward", promote.UNAVAILABLE, ["no forward ledger provided"], {}
    )
    verdict, reasons = promote.decide_promotion(pass_tier, unavailable_tier)
    assert verdict == promote.NOT_PROMOTED
    assert any("forward" in r for r in reasons)


def test_run_promotion_end_to_end_does_not_promote_without_forward_evidence(
    tmp_path, monkeypatch
) -> None:
    """Full run_promotion(), with the subprocess-spawning frozen/forward runners monkeypatched
    to canned, clearly-improving data (so nothing here spawns the agent CLI) — proves the same
    "helps on frozen, no forward evidence => not promoted" rule holds through the real pipeline,
    not just the pure decide_promotion() unit above."""
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "worldline-01.csv").write_text("# fixture, not a real recording\n")

    def fake_frozen(candidate, scenarios, root, live, schema):
        is_challenger = candidate.name != "champion"
        pnl = [1.0] * len(scenarios) if is_challenger else [0.0] * len(scenarios)
        return _metrics(pnl, [0.0] * len(scenarios), [0.0] * len(scenarios))

    monkeypatch.setattr(promote, "run_candidate_frozen", fake_frozen)

    champion = promote.Candidate("champion", "v1", 0.6)
    challenger = promote.Candidate("challenger", "v1", 0.5)
    record = promote.run_promotion(
        champion,
        challenger,
        ROOT,
        scenarios_dir,
        None,
        None,  # no champion forward ledger
        None,  # no challenger forward ledger
        _default_policy(),
        live=False,
    )
    assert record.frozen["verdict"] == promote.PASS
    assert record.forward["verdict"] == promote.UNAVAILABLE
    assert record.verdict == promote.NOT_PROMOTED


# --- correlated-errors note ----------------------------------------------------------------------


def test_correlated_errors_note_present_when_models_match(tmp_path, monkeypatch) -> None:
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "worldline-01.csv").write_text("# fixture\n")

    def fake_frozen(candidate, scenarios, root, live, schema):
        return _metrics(
            [1.0] * len(scenarios),
            [0.0] * len(scenarios),
            [0.0] * len(scenarios),
            model="fake-rule-v1",
        )

    monkeypatch.setattr(promote, "run_candidate_frozen", fake_frozen)
    record = promote.run_promotion(
        promote.Candidate("champion", "v1", 0.6),
        promote.Candidate("challenger", "v1", 0.5),
        ROOT,
        scenarios_dir,
        None,
        None,
        None,
        _default_policy(),
        live=False,
    )
    assert record.same_base_model is True
    assert "correlated" in record.correlated_errors_note.lower()
    assert "fake-rule-v1" in record.correlated_errors_note


def test_correlated_errors_flag_is_false_for_different_models(tmp_path, monkeypatch) -> None:
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "worldline-01.csv").write_text("# fixture\n")

    def fake_frozen(candidate, scenarios, root, live, schema):
        model = "champion-model" if candidate.name == "champion" else "challenger-model"
        return _metrics(
            [1.0] * len(scenarios), [0.0] * len(scenarios), [0.0] * len(scenarios), model=model
        )

    monkeypatch.setattr(promote, "run_candidate_frozen", fake_frozen)
    record = promote.run_promotion(
        promote.Candidate("champion", "v1", 0.6),
        promote.Candidate("challenger", "v1", 0.5),
        ROOT,
        scenarios_dir,
        None,
        None,
        None,
        _default_policy(),
        live=False,
    )
    assert record.same_base_model is False


# --- file-writing invariants: graveyard, recommendations, and "never touches the live prompt" ---


def test_append_graveyard_row_creates_header_once_and_appends(tmp_path) -> None:
    path = tmp_path / "graveyard.md"
    record = promote.PromotionRecord(
        ts="2026-09-28T00:00:00+00:00",
        champion={"name": "champion", "prompt_version": "v1", "prob_threshold": 0.6},
        challenger={"name": "challenger", "prompt_version": "v1", "prob_threshold": 0.5},
        frozen={"verdict": "FAIL", "metrics": {}, "reasons": ["did not improve"], "tier": "frozen"},
        forward={"verdict": "UNAVAILABLE", "metrics": {}, "reasons": [], "tier": "forward"},
        same_base_model=True,
        correlated_errors_note="note",
        verdict=promote.NOT_PROMOTED,
        reasons=["frozen tier verdict is FAIL: did not improve"],
    )
    promote.append_graveyard_row(record, path)
    promote.append_graveyard_row(record, path)
    text = path.read_text()
    assert text.count("# Agent challenger graveyard") == 1
    assert text.count("challenger") >= 2  # header mentions it once, plus one row per call


def test_write_recommendation_states_it_is_not_applied_automatically(tmp_path) -> None:
    record = promote.PromotionRecord(
        ts="2026-09-28T00:00:00+00:00",
        champion={"name": "champion", "prompt_version": "v1", "prob_threshold": 0.6},
        challenger={"name": "challenger", "prompt_version": "v1", "prob_threshold": 0.5},
        frozen={
            "verdict": "PASS",
            "reasons": [],
            "tier": "frozen",
            "metrics": {
                "net_pnl_after_costs_usd": {
                    "metric": "net_pnl_after_costs_usd",
                    "champion_mean": 0.0,
                    "challenger_mean": 1.0,
                    "observed_diff": 1.0,
                    "ci_lo": 0.5,
                    "ci_hi": 1.5,
                    "p_value": 0.0,
                    "holm_significant_improved": True,
                    "holm_significant_worsened": False,
                }
            },
        },
        forward={"verdict": "PASS", "metrics": {}, "reasons": [], "tier": "forward"},
        same_base_model=False,
        correlated_errors_note="",
        verdict=promote.PROMOTED,
        reasons=[],
    )
    path = promote.write_recommendation(record, tmp_path)
    text = path.read_text()
    assert "recommendation only" in text.lower()
    assert "nothing in this repository was changed" in text.lower()


def _ledger_entry(request_id: str, ts_ns: int, mid: str, action: str = "no_trade") -> dict:
    decision = {"request_id": request_id, "action": action, "rationale": "fixture"}
    result = None
    if action in ("buy", "sell"):
        decision["qty"] = "1"
        decision["limit_price"] = mid
        result = {"accepted": True}
    return {
        "request": {"request_id": request_id, "ts_ns": ts_ns, "mid": mid},
        "decision": decision,
        "result": result,
        "mode": "live",
        "prompt_version": "v1",
        "model": "unspecified",
    }


def _write_forward_ledger(path: Path, mids: list[str], first_action: str) -> None:
    entries = [
        _ledger_entry(f"f{i}", i, mid, first_action if i == 0 else "no_trade")
        for i, mid in enumerate(mids)
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_no_promotion_run_ever_mutates_the_live_prompt_or_decider_defaults(
    tmp_path, monkeypatch
) -> None:
    """The hard "never changes the live prompt itself" invariant, checked at the filesystem
    level: run_promotion() end to end (subprocess calls monkeypatched away) for both a genuine
    PROMOTED outcome (real forward-ledger fixtures, not just monkeypatched frozen data) and a
    NOT_PROMOTED one, asserting agent/src/prompts/decide-v1.md and
    agent/src/decider/{fake,live}.ts are byte-identical before and after every run."""
    watched = [
        ROOT / "agent/src/prompts/decide-v1.md",
        ROOT / "agent/src/decider/fake.ts",
        ROOT / "agent/src/decider/live.ts",
    ]
    before = {p: p.read_bytes() for p in watched}

    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "worldline-01.csv").write_text("# fixture\n")

    def fake_frozen_promoted(candidate, scenarios, root, live, schema):
        is_challenger = candidate.name != "champion"
        pnl = [1.0] * len(scenarios) if is_challenger else [0.0] * len(scenarios)
        return _metrics(pnl, [0.0] * len(scenarios), [0.0] * len(scenarios))

    monkeypatch.setattr(promote, "run_candidate_frozen", fake_frozen_promoted)
    out_dir = tmp_path / "promotions"

    # Real forward-ledger fixtures: champion never trades (flat equity); challenger buys once
    # and rides a steady rally (equity deltas 0, 10, 10, 10, 10) -- a real, large, genuinely
    # computed improvement, not just monkeypatched frozen-tier data.
    champion_forward = tmp_path / "champion-forward.jsonl"
    challenger_forward = tmp_path / "challenger-forward.jsonl"
    _write_forward_ledger(champion_forward, ["100", "100", "100", "100", "100"], "no_trade")
    _write_forward_ledger(challenger_forward, ["100", "110", "120", "130", "140"], "buy")

    verdicts_seen = set()
    for c_fwd, x_fwd in ((None, None), (champion_forward, challenger_forward)):
        record = promote.run_promotion(
            promote.Candidate("champion", "v1", 0.6),
            promote.Candidate("challenger", "v1", 0.5),
            ROOT,
            scenarios_dir,
            None,
            c_fwd,
            x_fwd,
            _default_policy(),
            live=False,
        )
        verdicts_seen.add(record.verdict)
        out_dir.mkdir(parents=True, exist_ok=True)
        if record.verdict == promote.PROMOTED:
            promote.write_recommendation(record, out_dir)
        else:
            promote.append_graveyard_row(record, out_dir / "graveyard.md")

    # Both branches of the invariant actually ran (otherwise this test would only prove the
    # NOT_PROMOTED path never mutates anything, which test_run_promotion_end_to_end_... already
    # covers on its own).
    assert verdicts_seen == {promote.NOT_PROMOTED, promote.PROMOTED}

    after = {p: p.read_bytes() for p in watched}
    assert before == after
