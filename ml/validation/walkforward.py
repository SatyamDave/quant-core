"""`just walkforward <name> [--record]`: purged, embargoed walk-forward of the baseline model.

Reads backtest/configs/<name>.yaml, builds the point-in-time dataset, evaluates every config
in the grid on identical folds, records each as a trial (failures included), computes deflated
Sharpe with the cumulative registry trial count and PBO, registers the best config's exported
artifact (a challenger only on PASS, otherwise no_signal), and writes a report with Assumptions
first.

Outputs go to the gitignored scratch directory out/walkforward/<name>/ (report, trial and model
ledger, data, artifact), which each run replaces. Only `--record` writes the canonical registry
(registry.REGISTRY_DIR) and backtest/reports/<name>/<date>.md.

This is a label-level evaluation (enter at the touch, exit after the label horizon), not an
execution backtest: latency and queue position are not simulated yet. The report says so.
"""

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from data.recorders.synthetic import TICK, write_day
from ml.datasets import DOWN, UP, Dataset, build, replay_top_of_book
from ml.export import export
from ml.features import FEATURE_NAMES, FEATURE_VERSION
from ml.registry import MODELS, ModelRecord, Status, append
from ml.training import TrainConfig, train
from ml.validation import deflated_sharpe, pbo, purged_walk_forward, sharpe
from registry import TRIALS, Trial, read, record, trial_count

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ("costs", "latency", "fill_model")
# Walk-forward -> paper thresholds from strategies/_template/GATES.md. deflated_sharpe returns a
# probability, so GATE_DSR = 1.0 is met only when the normal CDF saturates (z above about 8.3):
# it fails closed until a human sets the threshold as a probability.
GATE_DSR, GATE_PBO, GATE_POSITIVE_FOLDS = 1.0, 0.20, 0.70


@dataclass(frozen=True)
class ConfigResult:
    tc: TrainConfig
    returns: np.ndarray  # net bps per evaluation point, all folds in time order
    config_hash: str
    metrics: dict[str, float]


def load_config(name: str) -> dict[str, Any]:
    cfg: dict[str, Any] = yaml.safe_load((ROOT / "backtest/configs" / f"{name}.yaml").read_text())
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing or not cfg["costs"].get("fees"):
        raise SystemExit(f"refusing {name}: config lacks {missing or ['costs.fees']}")
    return cfg


def trade_returns(ds: Dataset, samples: np.ndarray, pred: np.ndarray, taker_bps: float) -> Any:
    """Net return in bps of taking the predicted side at the touch and exiting after the horizon.

    Cost is half the spread at entry and at exit plus taker fees on both legs.
    """
    h = int(str(ds.params["horizon"]))
    r = ds.rows[samples]
    pos = np.where(pred == UP, 1.0, np.where(pred == DOWN, -1.0, 0.0))
    mid2 = ds.mid2[r].astype(np.float64)
    gross = pos * (ds.mid2[r + h] - ds.mid2[r]) / mid2 * 1e4
    cost = np.abs(pos) * ((ds.spread[r] + ds.spread[r + h]) / mid2 * 1e4 + 2 * taker_bps)
    return gross - cost


def max_drawdown(returns: np.ndarray) -> float:
    equity = np.cumsum(returns)
    return float(np.max(np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:] - equity))


def verdict_of(gates: dict[str, bool | None]) -> str:
    """PASS only when every gate was evaluated and met; None means not evaluated."""
    return "PASS" if all(ok is True for ok in gates.values()) else "FAIL"


def registration(verdict: str) -> tuple[Status, str]:
    """A failed walk-forward is registered as no_signal, which promote() refuses."""
    return ("challenger", "") if verdict == "PASS" else ("no_signal", "walk-forward FAIL")


def git_rev() -> str:
    out = subprocess.run(
        ["git", "describe", "--always", "--dirty"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or "unknown"


@dataclass(frozen=True)
class Outputs:
    trials: Path
    models: Path
    reports: Path  # <reports>/<name>/<date>.md
    data: Path
    artifacts: Path  # <artifacts>/<name>/<sha>.json


def outputs(name: str, record_to_registry: bool) -> Outputs:
    if record_to_registry:
        reports = ROOT / "backtest/reports"
        return Outputs(TRIALS, MODELS, reports, ROOT / "data/raw", reports / "raw")
    scratch = ROOT / "out/walkforward" / name
    shutil.rmtree(scratch, ignore_errors=True)
    return Outputs(
        scratch / "registry/trials.jsonl",
        scratch / "registry/models.jsonl",
        scratch / "reports",
        scratch / "data",
        scratch / "artifacts",
    )


def run(name: str, record_to_registry: bool = False) -> Path:
    # The name becomes a path under backtest/configs and out/, so it may not climb out of them.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise SystemExit(f"refusing config name {name!r}: letters, digits, '-' and '_' only")
    cfg = load_config(name)
    out = outputs(name, record_to_registry)
    data, label, val, model_cfg = cfg["data"], cfg["label"], cfg["validation"], cfg["model"]
    taker_bps = float(cfg["costs"]["fees"]["taker_bps"])
    day = write_day(out.data / "synthetic", data["seed"], data["events"], data["date"])
    books = replay_top_of_book(day / "l2_delta.parquet")
    h = int(label["horizon_events"])
    ds = build(books, TICK, h, source=f"synthetic seed={data['seed']} events={data['events']}")
    folds = purged_walk_forward(
        len(ds.y), val["n_splits"], h, val["embargo_events"], val["min_train_events"]
    )
    # Evaluate one non-overlapping trade opportunity every `h` samples of each test block.
    evals = [test[::h] for _, test in folds]
    results: list[ConfigResult] = []
    for C in model_cfg["grid_C"]:
        tc = TrainConfig(C=float(C), seed=int(model_cfg["seed"]))
        per_fold = []
        for (train_idx, _), ev in zip(folds, evals, strict=True):
            m = train(ds.X[train_idx], ds.y[train_idx], tc)
            per_fold.append(trade_returns(ds, ev, m.predict(ds.X[ev]), taker_bps))
        rets = np.concatenate(per_fold)
        full = {**tc.as_dict(), "horizon": h, "dataset": ds.hash, "config": cfg}
        results.append(
            ConfigResult(
                tc=tc,
                returns=rets,
                config_hash=hashlib.sha256(json.dumps(full, sort_keys=True).encode()).hexdigest(),
                metrics={
                    "sharpe": sharpe(rets),
                    "trades": float(np.count_nonzero(rets)),
                    "net_bps": float(rets.sum()),
                    "max_drawdown_bps": max_drawdown(rets),
                    "turnover": float(np.count_nonzero(rets) / len(rets)),
                    "positive_folds": float(np.mean([f.sum() > 0 for f in per_fold])),
                },
            )
        )
    best = max(results, key=lambda r: r.metrics["sharpe"])
    rets = best.returns
    perf_pbo = pbo(np.column_stack([r.returns for r in results]), val["pbo_blocks"])
    # Every config in this run counts as a trial, on top of all earlier ones. Earlier trials come
    # from the canonical ledger even in scratch mode, whose own ledger is wiped each run.
    earlier = [t["metrics"]["sharpe"] for t in read(TRIALS) if t["experiment"] == name]
    all_sr = earlier + [r.metrics["sharpe"] for r in results]
    n_recorded = trial_count(TRIALS, name)
    n_trials = n_recorded + len(results)
    sd = float(np.std(rets))
    skew = float(np.mean((rets - rets.mean()) ** 3) / sd**3) if sd > 0 else 0.0
    kurt = float(np.mean((rets - rets.mean()) ** 4) / sd**4) if sd > 0 else 3.0
    dsr = deflated_sharpe(
        best.metrics["sharpe"], len(rets), n_trials, float(np.var(all_sr, ddof=1)), skew, kurt
    )
    gates: dict[str, bool | None] = {
        f"deflated Sharpe >= {GATE_DSR}": dsr >= GATE_DSR,
        f"PBO <= {GATE_PBO}": perf_pbo <= GATE_PBO,
        f"positive OOS folds >= {GATE_POSITIVE_FOLDS:.0%}": best.metrics["positive_folds"]
        >= GATE_POSITIVE_FOLDS,
        # Needs a backtest max drawdown to compare against, which this run does not produce.
        "max drawdown <= 1.5x backtest max drawdown": None,
    }
    verdict = verdict_of(gates)
    status, reason = registration(verdict)
    for r in results:
        passed = r is best and verdict == "PASS"
        record(out.trials, Trial(name, r.config_hash, passed, r.metrics, notes="walkforward"))

    final = train(ds.X, ds.y, best.tc)
    artifact, sha = export(final)
    art_path = out.artifacts / name / f"{sha[:16]}.json"
    art_path.parent.mkdir(parents=True, exist_ok=True)
    art_path.write_bytes(artifact)
    metrics = {**best.metrics, "deflated_sharpe": dsr, "pbo": perf_pbo}
    append(
        ModelRecord(
            experiment=name,
            artifact_sha256=sha,
            status=status,
            dataset_hash=ds.hash,
            feature_version=FEATURE_VERSION,
            config={**best.tc.as_dict(), "horizon": h, "trained_on": "all samples"},
            metrics=metrics,
            lineage={"git": git_rev(), "trial": best.config_hash, "parent": ""},
            reason=reason,
            verdict=verdict,
        ),
        out.models,
    )

    today = dt.datetime.now(dt.UTC).date().isoformat()
    report = out.reports / name / f"{today}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(
        f"| {r.tc.C} | {r.metrics['sharpe']:.4f} | {int(r.metrics['trades'])} "
        f"| {r.metrics['net_bps']:.1f} | {r.metrics['max_drawdown_bps']:.1f} "
        f"| {r.metrics['turnover']:.3f} | {r.metrics['positive_folds']:.0%} |"
        for r in results
    )
    mode = (
        "recorded to the canonical registry"
        if record_to_registry
        else "scratch run, not evidence; trials not recorded in the canonical registry "
        "(`--record` records)"
    )
    gate_rows = "\n".join(
        f"| {g} | {'not evaluated' if ok is None else 'met' if ok else 'not met'} |"
        for g, ok in gates.items()
    )
    report.write_text(f"""# Walk-forward: {name} ({today})

## Assumptions

- Data: synthetic day from `data/recorders/synthetic.py` (seed {data["seed"]}, \
{data["events"]} events, {data["date"]}). Not market data; results say nothing about any venue.
- Fills: `{cfg["fill_model"]["model"]}`. Every signal takes the touch at the signal event and \
exits at the touch {h} events later. Queue position and partial fills are not modeled.
- Costs: half the spread at entry and at exit, plus taker fee {taker_bps} bps on each leg. \
Funding is not applied (holds last seconds). Slippage beyond the touch is not modeled.
- Latency: configured (`{cfg["latency"]["model"]}`, order entry \
{cfg["latency"]["order_entry_ns"]} ns, market data {cfg["latency"]["market_data_ns"]} ns) but \
not simulated; signals act on the same event.
- Sharpe is per trade opportunity (one every {h} events, non-overlapping), not annualized.
- Folds: {val["n_splits"]} expanding walk-forward folds, purged by the {h}-event label horizon \
and embargoed {val["embargo_events"]} events; first {val["min_train_events"]} samples train only.
- Trials: deflated Sharpe uses {n_trials} trials for `{name}`: {n_recorded} from the canonical \
registry `{rel(TRIALS)}` plus this run's {len(results)}, failures included.

## Data

- Dataset hash: `{ds.hash}`
- Feature version: `{FEATURE_VERSION}` ({", ".join(FEATURE_NAMES)})
- Samples: {len(ds.y)}; labels down/flat/up: {np.bincount(ds.y + 1, minlength=3).tolist()}

## Results per config (identical folds)

| C | Sharpe | trades | net bps | max DD bps | turnover | positive folds |
|---|---|---|---|---|---|---|
{rows}

- Best config: C={best.tc.C}; deflated Sharpe {dsr:.4f}; PBO (CSCV, \
{val["pbo_blocks"]} blocks) {perf_pbo:.4f}.

## Gates (walk-forward to paper, strategies/_template/GATES.md)

| Gate | Result |
|---|---|
{gate_rows}

Verdict: **{verdict}**{"" if record_to_registry else " (scratch run, not evidence)"}.

## Registry

- Mode: {mode}.
- Trials: {len(results)} appended to `{rel(out.trials)}` (experiment `{name}`).
- Model: artifact sha256 `{sha}` in `{rel(out.models)}`, \
status `{status}`, verdict `{verdict}`, lineage git `{git_rev()}`. Artifact file (gitignored): \
`{rel(art_path)}`.
""")
    return report


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) not in (1, 2) or (len(args) == 2 and args[1] != "--record"):
        raise SystemExit("usage: walkforward <name> [--record]")
    print(run(args[0], record_to_registry=len(args) == 2))
