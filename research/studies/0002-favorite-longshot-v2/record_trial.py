"""Append this study's single registered trial to the research registry (run once).

Copied with attribution from research/studies/0001-favorite-longshot/record_trial.py,
adjusted for this study's experiment name.
"""

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from registry import TRIALS, Trial, record  # noqa: E402

EXPERIMENT = "study-0002-favorite-longshot-v2"

config_hash = hashlib.sha256((HERE / "config.json").read_bytes()).hexdigest()
if TRIALS.exists() and any(
    json.loads(line)["config_hash"] == config_hash for line in TRIALS.read_text().splitlines()
):
    sys.exit("trial for this frozen config already recorded")
res = json.loads((HERE / "results.json").read_text())
if "blocked" in res:
    metrics = {
        "analysed_markets": 0.0,
        "listing_requests": float(res["sample"]["listing"]["listing_requests_through"]),
    }
else:
    p, s = res["primary"], res["secondary"]
    metrics = {
        "G": p["G"],
        "G_ci_low": p["ci"][0],
        "G_ci_high": p["ci"][1],
        "mue_unverified": p["mue_UNVERIFIED_fees"],
        "pooled_events": float(p["pooled_events"]),
        "pooled_markets": float(p["pooled_markets"]),
    }
    for k in ("a", "brier_raw", "brier_candidate", "raw_minus_candidate"):
        if k in s:
            metrics[k] = s[k]
record(
    TRIALS,
    Trial(
        EXPERIMENT,
        config_hash,
        passed=res["verdict"] == "PROMOTE TO NEXT STAGE",
        metrics=metrics,
        notes=f"{res['verdict']}; {res.get('blocked', 'measured')}; "
        "real Kalshi public data, not a demo or fixture; population bounded to 4 series, "
        "sized with metadata-only /events counts before registration "
        "(redo of study 0001, issue #68); "
        f"results.json sha256 {hashlib.sha256((HERE / 'results.json').read_bytes()).hexdigest()}",
    ),
)
print(f"recorded {EXPERIMENT} -> {TRIALS}")
