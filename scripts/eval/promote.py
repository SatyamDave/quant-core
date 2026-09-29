#!/usr/bin/env python3
"""Issue #53: only promote agent (prompt / tool-config) changes that prove better, on both a
frozen suite AND a real forward test — never on just one, and never automatically.

  scripts/eval/promote.py --challenger-prob-threshold 0.55 [options...]

## What this compares

A "candidate" is a decider setting: `prompt_version` (which `agent/src/prompts/decide-<v>.md` the
live decider would use) and `prob_threshold` (`decider/fake.ts`'s `QC_FAKE_PROB_THRESHOLD` —
today's one env-injectable "tool-config" knob). The champion defaults to whatever is actually
committed (`PROMPT_VERSION` in `agent/src/decider/live.ts`, `DEFAULT_PROB_THRESHOLD` in
`agent/src/decider/fake.ts`, parsed from source so this can never silently drift from what the
agent actually runs — `read_champion_defaults()`).

## Two tiers, both required (issue #53's own acceptance criterion)

1. **Frozen** — champion and challenger each run once per scenario in `evals/scenarios/*.csv`
   plus `evals/scenarios/from_ledger/*.csv` (issue #52's generator; this is the loop closing),
   in fake mode (deterministic, free) by default, or live mode when `--live` is passed AND
   `scripts/ci/ai_gate.py loop-agent-eval` allows AND `ANTHROPIC_API_KEY` is set (otherwise
   `--live` is refused with the reason printed, never silently downgraded to fake). Compared
   *paired* per scenario (issue's pre-registered metric): net P&L after costs vs. no-trade
   (`scripts/reports/pnl_report.py`'s own fill accounting), plus two guardrails — invalid
   decisions (independently re-checked against `schemas/decision/v1/decision.schema.json`, not a
   re-typed copy of it) and risk rejects — which the challenger must not make significantly worse.
2. **Forward** — an already-recorded shadow/paper ledger for each candidate
   (`--champion-forward-ledger` / `--challenger-forward-ledger`; issue #48's mechanism produces
   these, not this script — a 14-day live run cannot be spawned inline by a CI tool). Without
   both, this tier is `UNAVAILABLE`, never a silent `PASS` ("a check that could not run is not a
   check that passed" — the same contract as `scripts/ci/ai_gate.py` and `scripts/eval/run.py`).
   Compared *unpaired* (two independent runs, not the same scenarios): per-decision P&L deltas
   and per-decision invalid/reject indicators, bootstrapped the same way.

Each metric's significance is a one-sided bootstrap test (`--seed`-deterministic
`random.Random`), Holm-corrected jointly across the metric family (`holm_reject()`) at
`evals/promotions/policy.toml`'s `alpha` — per `llm-trading-eval-research.md` §4's Ordinal-Gates
critique: an uncorrected single test can look like a real effect and evaporate under correction.

**PROMOTED requires both tiers PASS.** Frozen PASS + forward UNAVAILABLE is NOT_PROMOTED — this
exact case is tested directly
(`test_promote.py::test_frozen_pass_forward_unavailable_is_not_promoted`)
per issue #53's own acceptance criterion. Every run appends one record to
`evals/promotions/promotions.jsonl` (via `research/registry`'s generic append store — one
implementation, not a second copy) with both candidates' scores side by side, and a
`NOT_PROMOTED` verdict also appends one row to `evals/graveyard.md`. A `PROMOTED` verdict writes
*only* a recommendation file under `evals/promotions/recommendations/` for a maintainer to act on by
hand — **this script never edits `agent/src/prompts/*.md` or any decider default itself**
(tested: `test_no_file_ever_mutates_the_live_prompt_or_decider_defaults`).

## Correlated errors (issue #53's third acceptance criterion)

If champion and challenger report the same `model` field in their ledgers (in fake mode this is
always true — `fake-rule-v1` either way, since only the threshold differs, which is if anything
*more* correlated than two different LLM prompts), the record always says so explicitly and
names why agreement is not independent confirmation (Kim et al., arXiv:2506.07962,
`llm-trading-eval-research.md` §2) — never silently treated as two independent runs agreeing.

Standard library only, matching every other script in scripts/eval and scripts/reports.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import statistics
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = ROOT / "evals/promotions/policy.toml"
GRAVEYARD = ROOT / "evals/graveyard.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts/reports"))
sys.path.insert(0, str(ROOT / "research"))
import live_agent  # noqa: E402
import pnl_report  # noqa: E402

from registry import record as registry_record  # noqa: E402

PASS, FAIL, UNAVAILABLE = "PASS", "FAIL", "UNAVAILABLE"
PROMOTED, NOT_PROMOTED = "PROMOTED", "NOT_PROMOTED"

CORRELATED_ERRORS_NOTE = (
    "champion and challenger reported the same model ({model!r}); agreement between them here "
    "is not independent confirmation of anything beyond that one decision function's behavior "
    "at these two settings (correlated-errors caution: Kim, Garg, Peng & Garg, arXiv:2506.07962, "
    "llm-trading-eval-research.md §2 — two same-family models agree 60% of the time when both "
    "err, and larger/more-accurate models are *more* correlated, not less)."
)


@dataclass(frozen=True)
class Candidate:
    name: str
    prompt_version: str
    prob_threshold: float


def read_champion_defaults(root: Path = ROOT) -> Candidate:
    """Parses the committed defaults directly from source (never hardcoded here), so a champion
    definition can't silently drift from what the agent actually runs by default."""
    fake_text = (root / "agent/src/decider/fake.ts").read_text()
    live_text = (root / "agent/src/decider/live.ts").read_text()
    prob_match = re.search(r"DEFAULT_PROB_THRESHOLD\s*=\s*([0-9.]+)", fake_text)
    version_match = re.search(r'PROMPT_VERSION\s*=\s*"([^"]+)"', live_text)
    if not prob_match or not version_match:
        raise RuntimeError(
            "could not parse champion defaults from agent/src/decider/{fake,live}.ts "
            "(DEFAULT_PROB_THRESHOLD / PROMPT_VERSION) — refusing to guess a champion"
        )
    return Candidate("champion", version_match.group(1), float(prob_match.group(1)))


def load_decision_schema(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / "schemas/decision/v1/decision.schema.json").read_text())


def decision_is_valid(decision: dict[str, Any], schema: dict[str, Any]) -> bool:
    """Independently re-checks a ledger row's `decision` against the committed
    `decision.schema.json` (patterns and maxLength read from that file, not retyped here, so the
    two can never drift — same convention as scripts/eval/run.py's `required_ledger_keys`)."""
    props = schema["properties"]
    action = decision.get("action")
    if action not in props["action"]["enum"]:
        return False
    if not isinstance(decision.get("request_id"), str):
        return False
    if action in ("buy", "sell"):
        for key in ("qty", "limit_price"):
            value = decision.get(key)
            if not isinstance(value, str) or not re.fullmatch(props[key]["pattern"], value):
                return False
    rationale = decision.get("rationale")
    if not isinstance(rationale, str) or len(rationale) > props["rationale"]["maxLength"]:
        return False
    return True


def scenario_files(scenarios_dir: Path, extra_dir: Path | None = None) -> list[Path]:
    files = sorted(scenarios_dir.glob("*.csv"))
    if extra_dir is not None and extra_dir.exists():
        files += sorted(extra_dir.glob("*.csv"))
    return files


@dataclass
class ScenarioMetrics:
    net_pnl_after_costs_usd: list[float] = field(default_factory=list)
    invalid_decisions: list[float] = field(default_factory=list)
    risk_rejects: list[float] = field(default_factory=list)
    model: str | None = None


def _metrics_from_entries(
    entries: list[dict[str, Any]], schema: dict[str, Any]
) -> tuple[float, float, float, str | None]:
    result, _curve = pnl_report.compute(entries, fee_bps=Decimal("0"))
    net_pnl = float(Decimal(result.agent["total_pnl_after_fees_usd"]))
    invalid = sum(1 for e in entries if not decision_is_valid(e.get("decision", {}), schema))
    rejects = sum(
        1
        for e in entries
        if isinstance(e.get("result"), dict) and e["result"].get("accepted") is False
    )
    model = entries[-1].get("model") if entries else None
    return net_pnl, float(invalid), float(rejects), model


def run_candidate_frozen(
    candidate: Candidate, scenarios: list[Path], root: Path, live: bool, schema: dict[str, Any]
) -> ScenarioMetrics:
    """Runs `candidate` once per scenario (fake mode by default; live mode when the caller has
    already confirmed the gate allows it — see main()). One agent CLI invocation per scenario,
    reusing scripts/eval/live_agent.py's own subprocess plumbing rather than a second copy."""
    metrics = ScenarioMetrics()
    for scenario in scenarios:
        mode = "live" if live else "fake"
        # Fake mode varies the rule threshold (there is no prompt to vary); live mode is where a
        # prompt_version challenger actually differs from the champion (issue #54's
        # QC_PROMPT_VERSION override, agent/src/decider/live.ts) -- passed through unconditionally
        # so a champion whose prompt_version also isn't the committed default still gets pinned,
        # not silently left on whatever QC_PROMPT_VERSION happens to be set in this process's env.
        extra_env = (
            {"QC_PROMPT_VERSION": candidate.prompt_version}
            if live
            else {"QC_FAKE_PROB_THRESHOLD": str(candidate.prob_threshold)}
        )
        ledger_path = live_agent._run_agent_once(scenario, root, mode=mode, extra_env=extra_env)
        entries = pnl_report.load_entries(ledger_path)
        net_pnl, invalid, rejects, model = _metrics_from_entries(entries, schema)
        metrics.net_pnl_after_costs_usd.append(net_pnl)
        metrics.invalid_decisions.append(invalid)
        metrics.risk_rejects.append(rejects)
        if model is not None:
            metrics.model = model
    return metrics


def run_candidate_forward(
    ledger_path: Path | None, schema: dict[str, Any]
) -> ScenarioMetrics | None:
    """Per-decision metric lists from an already-recorded forward/shadow ledger (issue #48's
    output), or None if no ledger was provided or it is empty — the caller treats that as
    UNAVAILABLE, never as a pass."""
    if ledger_path is None:
        return None
    entries = pnl_report.load_entries(ledger_path)
    if not entries:
        return None
    _result, curve = pnl_report.compute(entries, fee_bps=Decimal("0"))
    deltas: list[float] = []
    prev = Decimal(0)
    for point in curve:
        deltas.append(float(point.agent_equity - prev))
        prev = point.agent_equity
    invalid = [0.0 if decision_is_valid(e.get("decision", {}), schema) else 1.0 for e in entries]
    rejects = [
        1.0 if isinstance(e.get("result"), dict) and e["result"].get("accepted") is False else 0.0
        for e in entries
    ]
    model = entries[-1].get("model")
    return ScenarioMetrics(
        net_pnl_after_costs_usd=deltas, invalid_decisions=invalid, risk_rejects=rejects, model=model
    )


def bootstrap_mean_diff(
    champion: list[float], challenger: list[float], paired: bool, n_boot: int, rng: random.Random
) -> tuple[float, float, float, float, float]:
    """Returns (observed diff, ci_lo, ci_hi, p_improve, p_worsen) for `challenger - champion`.

    `paired=True` (the frozen tier: same scenarios, same order) resamples the per-scenario
    *differences* — the correct paired bootstrap. `paired=False` (the forward tier: two
    independent ledgers of different length) resamples each side independently — an unpaired
    two-sample bootstrap.

    The caller sign-flips its inputs so "diff > 0" always means "improvement" for whatever
    metric is being tested (see `evaluate_tier`). `p_improve` (one-sided, H0: diff <= 0) and
    `p_worsen` (one-sided, H0: diff >= 0) are computed from the *same* bootstrap distribution,
    not derived as `1 - p_improve` — with a degenerate, zero-variance distribution (champion and
    challenger identical) both come out large (no evidence of improvement *or* worsening), which
    `1 - p_improve` would get wrong (it would call that case significant worsening).
    """
    if not champion or not challenger:
        return 0.0, 0.0, 0.0, 1.0, 1.0
    if paired:
        if len(champion) != len(challenger):
            raise ValueError("paired bootstrap needs equal-length, order-matched lists")
        diffs = [x - c for c, x in zip(champion, challenger, strict=True)]
        observed = statistics.fmean(diffs)
        boot = [statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(n_boot)]
    else:
        observed = statistics.fmean(challenger) - statistics.fmean(champion)
        boot = []
        for _ in range(n_boot):
            c_mean = statistics.fmean(rng.choices(champion, k=len(champion)))
            x_mean = statistics.fmean(rng.choices(challenger, k=len(challenger)))
            boot.append(x_mean - c_mean)
    boot.sort()
    lo = boot[int(0.025 * len(boot))]
    hi = boot[min(len(boot) - 1, int(0.975 * len(boot)))]
    p_improve = sum(1 for b in boot if b <= 0) / len(boot)
    p_worsen = sum(1 for b in boot if b >= 0) / len(boot)
    return observed, lo, hi, p_improve, p_worsen


def holm_reject(p_values: list[float], alpha: float) -> list[bool]:
    """Holm-Bonferroni step-down. Returns, in the caller's original order, which hypotheses are
    rejected (i.e. significant) at family-wise level `alpha`."""
    m = len(p_values)
    order = sorted(range(m), key=lambda idx: p_values[idx])
    rejected = [False] * m
    for rank, idx in enumerate(order):
        if p_values[idx] <= alpha / (m - rank):
            rejected[idx] = True
        else:
            break  # Holm stops at the first non-rejection
    return rejected


# (metric name, "higher"=improvement means the value rising, "lower"=improvement means falling)
METRIC_DIRECTIONS = (
    ("net_pnl_after_costs_usd", "higher"),
    ("invalid_decisions", "lower"),
    ("risk_rejects", "lower"),
)


@dataclass
class MetricResult:
    metric: str
    champion_mean: float
    challenger_mean: float
    observed_diff: float
    ci_lo: float
    ci_hi: float
    p_value: float
    holm_significant_improved: bool
    holm_significant_worsened: bool


@dataclass
class TierResult:
    tier: str
    verdict: str  # PASS | FAIL | UNAVAILABLE
    reasons: list[str]
    metrics: dict[str, MetricResult]


def evaluate_tier(
    tier: str,
    champion: ScenarioMetrics | None,
    challenger: ScenarioMetrics | None,
    paired: bool,
    min_improvement_usd: Decimal,
    alpha: float,
    n_boot: int,
    seed: int,
) -> TierResult:
    if champion is None or challenger is None:
        return TierResult(tier, UNAVAILABLE, [f"{tier}: no data for one or both candidates"], {})

    rng = random.Random(seed)  # noqa: S311 - deterministic resampling, not a crypto use
    improve_p: dict[str, float] = {}
    worsen_p: dict[str, float] = {}
    results: dict[str, MetricResult] = {}
    for name, direction in METRIC_DIRECTIONS:
        c_vals = getattr(champion, name)
        x_vals = getattr(challenger, name)
        if not c_vals or not x_vals:
            return TierResult(tier, UNAVAILABLE, [f"{tier}: metric {name} has no data"], {})
        sign = 1.0 if direction == "higher" else -1.0
        c_adj = [v * sign for v in c_vals]
        x_adj = [v * sign for v in x_vals]
        _observed_adj, lo, hi, p_improve, p_worsen = bootstrap_mean_diff(
            c_adj, x_adj, paired, n_boot, rng
        )
        improve_p[name] = p_improve
        worsen_p[name] = p_worsen
        results[name] = MetricResult(
            metric=name,
            champion_mean=statistics.fmean(c_vals),
            challenger_mean=statistics.fmean(x_vals),
            observed_diff=statistics.fmean(x_vals) - statistics.fmean(c_vals),
            ci_lo=lo * sign,
            ci_hi=hi * sign,
            p_value=p_improve,
            holm_significant_improved=False,  # filled in below, after joint Holm correction
            holm_significant_worsened=False,
        )

    # One joint Holm family across all metrics: the primary metric's "improved" test and both
    # guardrails' "worsened" test — per llm-trading-eval-research.md §4, correcting only within
    # one direction would understate the real number of comparisons being made.
    family_names = [f"{n}_improve" for n, _ in METRIC_DIRECTIONS] + [
        f"{n}_worsen" for n, _ in METRIC_DIRECTIONS
    ]
    family_p = [improve_p[n] for n, _ in METRIC_DIRECTIONS] + [
        worsen_p[n] for n, _ in METRIC_DIRECTIONS
    ]
    rejected = holm_reject(family_p, alpha)
    sig = dict(zip(family_names, rejected, strict=True))
    for name, _direction in METRIC_DIRECTIONS:
        results[name].holm_significant_improved = sig[f"{name}_improve"]
        results[name].holm_significant_worsened = sig[f"{name}_worsen"]

    reasons = []
    primary = results["net_pnl_after_costs_usd"]
    if not (
        Decimal(str(primary.observed_diff)) >= min_improvement_usd
        and primary.holm_significant_improved
    ):
        reasons.append(
            f"{tier}: net P&L improvement ${primary.observed_diff:.4f} does not clear "
            f"${min_improvement_usd} with Holm-corrected significance (p={primary.p_value:.4f})"
        )
    for name in ("invalid_decisions", "risk_rejects"):
        m = results[name]
        if m.holm_significant_worsened:
            reasons.append(
                f"{tier}: {name} significantly worse for the challenger "
                f"(champion={m.champion_mean:.3f}, challenger={m.challenger_mean:.3f})"
            )
    verdict = FAIL if reasons else PASS
    return TierResult(tier, verdict, reasons, results)


@dataclass
class PromotionRecord:
    ts: str
    champion: dict[str, Any]
    challenger: dict[str, Any]
    frozen: dict[str, Any]
    forward: dict[str, Any]
    same_base_model: bool
    correlated_errors_note: str
    verdict: str
    reasons: list[str]


def decide_promotion(frozen: TierResult, forward: TierResult) -> tuple[str, list[str]]:
    """PROMOTED only if both tiers PASS — a tier that could not run (UNAVAILABLE) is never
    treated as a pass, matching every other fail-closed gate in this repo."""
    reasons: list[str] = []
    if frozen.verdict != PASS:
        reasons.append(
            f"frozen tier verdict is {frozen.verdict}"
            + (f": {'; '.join(frozen.reasons)}" if frozen.reasons else "")
        )
    if forward.verdict != PASS:
        reasons.append(
            f"forward tier verdict is {forward.verdict}"
            + (f": {'; '.join(forward.reasons)}" if forward.reasons else "")
        )
    return (NOT_PROMOTED, reasons) if reasons else (PROMOTED, [])


def _tier_to_dict(t: TierResult) -> dict[str, Any]:
    return {
        "tier": t.tier,
        "verdict": t.verdict,
        "reasons": t.reasons,
        "metrics": {k: asdict(v) for k, v in t.metrics.items()},
    }


def append_graveyard_row(record: PromotionRecord, path: Path = GRAVEYARD) -> None:
    header = (
        "# Agent challenger graveyard\n\n"
        "Prompt/tool-config challengers that did not clear scripts/eval/promote.py's bar "
        "(issue #53). Check here before re-proposing a similar change. Every promotion or "
        "rejection is also recorded in full in `evals/promotions/promotions.jsonl`.\n\n"
        "| Date | Challenger | Champion | Frozen | Forward | Why | Record |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    if not path.exists():
        path.write_text(header)
    challenger_desc = (
        f"{record.challenger['name']} (prompt={record.challenger['prompt_version']}, "
        f"threshold={record.challenger['prob_threshold']})"
    )
    row = (
        f"| {record.ts[:10]} | {challenger_desc} "
        f"| {record.champion['name']} | {record.frozen['verdict']} | {record.forward['verdict']} "
        f"| {'; '.join(record.reasons) or '(see record)'} | `evals/promotions/promotions.jsonl` |\n"
    )
    with path.open("a", encoding="utf-8") as f:
        f.write(row)


def write_recommendation(record: PromotionRecord, out_dir: Path) -> Path:
    rec_dir = out_dir / "recommendations"
    rec_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", f"{record.ts}-{record.challenger['name']}")
    path = rec_dir / f"{slug}.md"
    correlated_note = (
        record.correlated_errors_note
        if record.same_base_model
        else "Champion and challenger reported different models; no correlated-errors caveat."
    )
    text = (
        f"""# Promotion recommendation: {record.challenger["name"]} (issue #53)

**This is a recommendation only.** Nothing in this repository was changed by generating it — no
file under `agent/src/prompts/` or any decider default was edited. A maintainer decides whether and
how to apply this by hand.

Generated {record.ts}.

## Candidates

| | champion | challenger |
|---|---|---|
| prompt_version | {record.champion["prompt_version"]} | {record.challenger["prompt_version"]} |
| prob_threshold | {record.champion["prob_threshold"]} | {record.challenger["prob_threshold"]} |

## Frozen suite

Verdict: **{record.frozen["verdict"]}**

| Metric | Champion | Challenger | Diff | Holm-significant |
|---|---|---|---|---|
"""
        + "\n".join(
            f"| {m['metric']} | {m['champion_mean']:.4f} | {m['challenger_mean']:.4f} "
            f"| {m['observed_diff']:.4f} | improved={m['holm_significant_improved']} "
            f"worsened={m['holm_significant_worsened']} |"
            for m in record.frozen["metrics"].values()
        )
        + f"""

## Forward (shadow/paper)

Verdict: **{record.forward["verdict"]}**

## Correlated errors

{correlated_note}
"""
    )
    path.write_text(text)
    return path


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)["promotion"]


def gate_allows_live(root: Path) -> tuple[bool, str]:
    allowed, reason = live_agent.gate_allows(root)
    if not allowed:
        return False, reason
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY not set"
    return True, "allowed"


def run_promotion(
    champion: Candidate,
    challenger: Candidate,
    root: Path,
    scenarios_dir: Path,
    extra_scenarios_dir: Path | None,
    champion_forward_ledger: Path | None,
    challenger_forward_ledger: Path | None,
    policy: dict[str, Any],
    live: bool,
) -> PromotionRecord:
    schema = load_decision_schema(root)
    seed = int(policy["seed"])
    alpha = float(policy["alpha"])
    n_boot = int(policy["n_bootstrap"])
    min_improvement = Decimal(str(policy["min_improvement_usd"]))

    scenarios = scenario_files(scenarios_dir, extra_scenarios_dir)
    if not scenarios:
        frozen = TierResult("frozen", UNAVAILABLE, ["no scenario files found"], {})
        champion_frozen = challenger_frozen = None
    else:
        champion_frozen = run_candidate_frozen(champion, scenarios, root, live, schema)
        challenger_frozen = run_candidate_frozen(challenger, scenarios, root, live, schema)
        frozen = evaluate_tier(
            "frozen", champion_frozen, challenger_frozen, True, min_improvement, alpha, n_boot, seed
        )

    champion_forward = run_candidate_forward(champion_forward_ledger, schema)
    challenger_forward = run_candidate_forward(challenger_forward_ledger, schema)
    forward = evaluate_tier(
        "forward", champion_forward, challenger_forward, False, min_improvement, alpha, n_boot, seed
    )

    verdict, reasons = decide_promotion(frozen, forward)

    champion_model = (champion_frozen.model if champion_frozen else None) or (
        champion_forward.model if champion_forward else None
    )
    challenger_model = (challenger_frozen.model if challenger_frozen else None) or (
        challenger_forward.model if challenger_forward else None
    )
    same_base_model = champion_model is not None and champion_model == challenger_model

    return PromotionRecord(
        ts=dt.datetime.now(dt.UTC).isoformat(),
        champion={**asdict(champion), "model": champion_model},
        challenger={**asdict(challenger), "model": challenger_model},
        frozen=_tier_to_dict(frozen),
        forward=_tier_to_dict(forward),
        same_base_model=same_base_model,
        correlated_errors_note=CORRELATED_ERRORS_NOTE.format(model=champion_model),
        verdict=verdict,
        reasons=reasons,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--challenger-name", default="challenger")
    parser.add_argument("--challenger-prompt-version", default=None)
    parser.add_argument("--challenger-prob-threshold", type=float, required=True)
    parser.add_argument("--champion-prompt-version", default=None)
    parser.add_argument("--champion-prob-threshold", type=float, default=None)
    parser.add_argument("--scenarios-dir", type=Path, default=None)
    parser.add_argument("--extra-scenarios-dir", type=Path, default=None)
    parser.add_argument("--champion-forward-ledger", type=Path, default=None)
    parser.add_argument("--challenger-forward-ledger", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)

    root: Path = args.root
    defaults = read_champion_defaults(root)
    champion = Candidate(
        "champion",
        args.champion_prompt_version or defaults.prompt_version,
        args.champion_prob_threshold
        if args.champion_prob_threshold is not None
        else defaults.prob_threshold,
    )
    challenger = Candidate(
        args.challenger_name,
        args.challenger_prompt_version or champion.prompt_version,
        args.challenger_prob_threshold,
    )
    if challenger == Candidate(challenger.name, champion.prompt_version, champion.prob_threshold):
        print(
            "refusing: challenger is identical to champion on every evaluated setting",
            file=sys.stderr,
        )
        return 2

    live = args.live
    if live:
        allowed, reason = gate_allows_live(root)
        if not allowed:
            print(
                f"--live refused: {reason} (falling back to fake mode would misreport a "
                "live result as gated; exiting instead)",
                file=sys.stderr,
            )
            return 1

    policy_path = args.policy or (root / "evals/promotions/policy.toml")
    policy = load_policy(policy_path)
    out_dir = args.out_dir or (root / "evals/promotions")
    scenarios_dir = args.scenarios_dir or (root / "evals/scenarios")
    extra_scenarios_dir = args.extra_scenarios_dir or (root / "evals/scenarios/from_ledger")

    record = run_promotion(
        champion,
        challenger,
        root,
        scenarios_dir,
        extra_scenarios_dir,
        args.champion_forward_ledger,
        args.challenger_forward_ledger,
        policy,
        live,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    registry_record(out_dir / "promotions.jsonl", record)

    print(f"verdict: {record.verdict}")
    for reason in record.reasons:
        print(f"  - {reason}")
    if record.same_base_model:
        print(f"note: {record.correlated_errors_note}")

    if record.verdict == NOT_PROMOTED:
        append_graveyard_row(record)
    else:
        rec_path = write_recommendation(record, out_dir)
        print(
            f"recommendation written to {rec_path} (maintainers act by hand; nothing auto-applied)"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
