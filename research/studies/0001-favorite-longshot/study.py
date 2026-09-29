"""Study 0001: favorite-longshot bias. Selection, features, split, metrics and decision.

Runs offline from data/raw/study-0001 and writes RESULTS.md and results.json next to it.
Refuses to run unless that snapshot matches the sha256 of every file in the catalog
(data/catalog/study-0001.json): any other snapshot is a different experiment, not run 0001.
Labels live in a separate mapping and are joined only inside the metric functions.
"""

import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RAW = ROOT / "data" / "raw" / "study-0001"
CATALOG = ROOT / "data" / "catalog" / "study-0001.json"
REPRODUCIBILITY = (
    "NOT REPRODUCIBLE FROM A CLEAN CLONE; reproducible from the preserved snapshot. "
    "The raw snapshot is gitignored and held outside git by whoever ran it "
    "(not an approved shared store); data/catalog/study-0001.json holds its hashes "
    "(see README.md)."
)
CONFIG: dict[str, Any] = json.loads((HERE / "config.json").read_text())
SEED: int = CONFIG["seed"]

FEATURE_KEYS = frozenset(
    {
        "ticker",
        "event_ticker",
        "series",
        "decision_ts",
        "price",
        "price_ts",
        "fee_multiplier",
        "fee_waived",
        "window_volume",
    }
)
LABEL_KEYS = frozenset({"result", "settlement_ts", "settlement_value_dollars", "expiration_value"})


def ts(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def h(s: str) -> str:
    return hashlib.sha256(f"{SEED}:{s}".encode()).hexdigest()


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot_problems(raw: Path = RAW, catalog: Path = CATALOG) -> list[str]:
    """Every way `raw` differs from the catalogued snapshot; empty means it is the same one."""
    cat = json.loads(catalog.read_text())
    expected: dict[str, str] = cat["files"]
    if not raw.is_dir():
        return [f"{raw} does not exist"]
    problems = []
    for rel, want in sorted(expected.items()):
        f = raw / rel
        if not f.is_file():
            problems.append(f"missing {rel} (catalog sha256 {want})")
        elif (got := sha256_file(f)) != want:
            problems.append(f"{rel}: sha256 {got}, catalog {want}")
    for f in sorted(raw.rglob("*.json")):
        if str(f.relative_to(raw)) not in expected:
            problems.append(f"{f.relative_to(raw)} is not in the catalog")
    ledger = raw / "requests.jsonl"
    if not ledger.is_file():
        problems.append(f"missing requests.jsonl (catalog sha256 {cat['ledger_sha256']})")
    elif (got := sha256_file(ledger)) != cat["ledger_sha256"]:
        problems.append(f"requests.jsonl: sha256 {got}, catalog {cat['ledger_sha256']}")
    return problems


def require_snapshot(raw: Path) -> None:
    problems = snapshot_problems(raw)
    if not problems:
        return
    if not raw.is_dir():
        files = json.loads(CATALOG.read_text())["files"]
        listing = "\n".join(f"  {sha}  {rel}" for rel, sha in sorted(files.items()))
        msg = (
            f"study-0001: snapshot missing: {raw} does not exist.\n{REPRODUCIBILITY}\n"
            f"Run 0001 needs exactly these files (sha256 from {CATALOG.relative_to(ROOT)}):\n"
            f"{listing}\n"
            "A re-fetch is a different snapshot, not this experiment."
        )
    else:
        msg = (
            f"study-0001: {raw} does not match {CATALOG.relative_to(ROOT)}, so it is a different "
            "snapshot, not run 0001; results.json was not touched:\n  " + "\n  ".join(problems)
        )
    print(msg, file=sys.stderr)
    sys.exit(2)


# ---------- selection (uses listing metadata only; result is read solely to drop non yes/no)


def load_listing(raw: Path = RAW) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest = json.loads((raw / "listing" / "manifest.json").read_text())
    markets: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for f in sorted((raw / "listing").glob("day_*.json")):
        for m in json.loads(f.read_text())["markets"]:
            if m["ticker"] in markets:
                duplicates += 1
            markets[m["ticker"]] = m
    manifest["duplicate_tickers"] = duplicates
    return sorted(markets.values(), key=lambda m: m["ticker"]), manifest


def decision_ts(m: dict[str, Any]) -> int:
    return ts(m["expected_expiration_time"]) - CONFIG["decision"]["offset_hours"] * 3600


def eligible(markets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Registered population filters. Returns kept markets and exclusion counts."""
    out, why = [], Counter()
    closed_before_decision = Counter()
    for m in markets:
        if m.get("market_type") != "binary":
            why["not_binary"] += 1
        elif m.get("result") not in ("yes", "no"):
            why[f"result_{m.get('result') or 'empty'}"] += 1
        elif m.get("mve_collection_ticker"):
            why["mve"] += 1
        elif float(m["volume_fp"]) <= 0:
            why["zero_volume"] += 1
        elif not m.get("expected_expiration_time"):
            why["no_expected_expiration_time"] += 1
        elif ts(m["open_time"]) >= decision_ts(m):
            why["opened_after_decision"] += 1
        elif ts(m["close_time"]) <= decision_ts(m):
            why["closed_before_decision"] += 1
            closed_before_decision[m["result"]] += 1
        elif not m.get("settlement_ts"):
            why["no_settlement_ts"] += 1
        else:
            out.append(m)
    return out, {
        "excluded": dict(why),
        "closed_before_decision_by_result": dict(closed_before_decision),
    }


def sample_order(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Events in sha256 order, up to max_markets_per_event markets each, in sha256 order."""
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in markets:
        by_event[m["event_ticker"]].append(m)
    cap = CONFIG["population"]["max_markets_per_event"]
    ordered = []
    for ev in sorted(by_event, key=h):
        ordered += sorted(by_event[ev], key=lambda m: h(m["ticker"]))[:cap]
    return ordered


def trade_windows(m: dict[str, Any], trades_cutoff: int) -> list[tuple[str, int, int]]:
    """(endpoint, min_ts, max_ts) requests covering [D - 24 h, D], split at the trades cutoff."""
    d = decision_ts(m)
    lo = d - CONFIG["price"]["window_hours_before_decision"] * 3600
    if lo >= trades_cutoff:
        return [("/markets/trades", lo, d)]
    if d < trades_cutoff:
        return [("/historical/trades", lo, d)]
    return [("/historical/trades", lo, trades_cutoff), ("/markets/trades", trades_cutoff, d)]


# ---------- features


def trade_file(ticker: str, endpoint: str) -> str:
    kind = "hist" if endpoint.startswith("/historical") else "live"
    return f"trades/{kind}/{ticker}.json"


def feature_row(
    m: dict[str, Any], trades: list[dict[str, Any]], series: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str]:
    """Feature row from trades at or before the decision time; None and a reason if excluded."""
    d = decision_ts(m)
    usable = [t for t in trades if ts(t["created_time"]) <= d]
    if not usable:
        return None, "no_trade_in_window"
    volume = sum(float(t["count_fp"]) for t in usable)
    if volume < CONFIG["price"]["min_window_volume_contracts"]:
        return None, "window_volume_below_min"
    last = max(usable, key=lambda t: ts(t["created_time"]))  # ties: first in API order
    s = series.get(m["event_ticker"].split("-")[0])
    waiver = m.get("fee_waiver_expiration_time")
    return {
        "ticker": m["ticker"],
        "event_ticker": m["event_ticker"],
        "series": s["ticker"] if s else None,
        "decision_ts": d,
        "price": float(last["yes_price_dollars"]),
        "price_ts": ts(last["created_time"]),
        "fee_multiplier": float(s["fee_multiplier"]) if s else None,
        "fee_waived": bool(waiver) and ts(waiver) > d,
        "window_volume": volume,
    }, "ok"


def build(raw: Path = RAW) -> dict[str, Any]:
    markets, listing_manifest = load_listing(raw)
    kept, exclusions = eligible(markets)
    fetched = json.loads((raw / "trades" / "sampled.json").read_text())
    by_ticker = {m["ticker"]: m for m in kept}
    series = {
        s["ticker"]: s for s in json.loads((raw / "series" / "series.json").read_text())["series"]
    }
    rows, labels, reasons, checks = [], {}, Counter(), Counter()
    for m in kept:
        if ts(m["settlement_ts"]) < ts(m["close_time"]):
            checks["settled_before_close"] += 1
        if ts(m["close_time"]) <= ts(m["open_time"]):
            checks["close_not_after_open"] += 1
    for entry in fetched["markets"]:
        m = by_ticker[entry["ticker"]]
        trades: list[dict[str, Any]] = []
        for f in entry["files"]:
            page = json.loads((raw / f).read_text())
            got = page["trades"]
            times = [ts(t["created_time"]) for t in got]
            checks["trade_pages_not_newest_first"] += times != sorted(times, reverse=True)
            checks["trade_pages_truncated_at_limit"] += bool(page.get("cursor"))
            trades += got
        ids = [t["trade_id"] for t in trades]
        checks["duplicate_trade_ids"] += len(ids) - len(set(ids))
        checks["trades_outside_open_close"] += sum(
            not ts(m["open_time"]) <= ts(t["created_time"]) <= ts(m["close_time"]) for t in trades
        )
        checks["trades_wrong_ticker"] += sum(t["ticker"] != m["ticker"] for t in trades)
        row, reason = feature_row(m, trades, series)
        reasons[reason] += 1
        if row:
            rows.append(row)
            labels[m["ticker"]] = {
                "y": 1 if m["result"] == "yes" else 0,
                "settlement_ts": ts(m["settlement_ts"]),
            }
    return {
        "rows": rows,
        "labels": labels,
        "listing": listing_manifest,
        "exclusions": exclusions,
        "eligible_markets": len(kept),
        "eligible_events": len({m["event_ticker"] for m in kept}),
        "sampled_markets": len(fetched["markets"]),
        "feature_reasons": dict(reasons),
        "sampling_stopped": fetched["stopped"],
        "validation": dict(checks)
        | {"duplicate_listing_tickers": listing_manifest["duplicate_tickers"]},
    }


# ---------- split and leakage guard


def split(rows: list[dict[str, Any]], labels: dict[str, dict[str, int]]) -> dict[str, Any]:
    ds = sorted(r["decision_ts"] for r in rows)
    s_cut = ds[
        min(len(ds) - 1, math.ceil(CONFIG["split"]["time_quantile_of_decision_ts"] * len(ds)) - 1)
    ]
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_event[r["event_ticker"]].append(r)
    train, test, purged = [], [], []
    for ev in sorted(by_event):
        rs = by_event[ev]
        if all(labels[r["ticker"]]["settlement_ts"] < s_cut for r in rs):
            train.append(ev)
        elif all(r["decision_ts"] >= s_cut for r in rs):
            test.append(ev)
        else:
            purged.append(ev)
    return {"S": s_cut, "train": train, "test": test, "purged": purged}


def check_no_leakage(
    rows: list[dict[str, Any]], labels: dict[str, dict[str, int]], sp: dict[str, Any]
) -> None:
    """Raise if a price postdates its decision time, a label field is in a feature row,
    or the train split's labels were not all public before the first test decision."""
    for r in rows:
        if r["price_ts"] > r["decision_ts"]:
            raise AssertionError(f"price after decision time: {r['ticker']}")
        leaked = set(r) & LABEL_KEYS or set(r) - FEATURE_KEYS
        if leaked:
            raise AssertionError(f"non-feature field {sorted(leaked)} in row {r['ticker']}")
    train, test = set(sp["train"]), set(sp["test"])
    if train & test:
        raise AssertionError("event in both train and test")
    tr_settle = [labels[r["ticker"]]["settlement_ts"] for r in rows if r["event_ticker"] in train]
    te_dec = [r["decision_ts"] for r in rows if r["event_ticker"] in test]
    if tr_settle and te_dec and max(tr_settle) >= min(te_dec):
        raise AssertionError("train labels settle after the first test decision")


# ---------- metrics


def primary_terms(
    rows: list[dict[str, Any]], labels: dict[str, dict[str, int]]
) -> list[tuple[str, float]]:
    """(event, s*(y-p)) for each pooled-bin market."""
    lo, hi = CONFIG["primary"]["low_bin_max_price"], CONFIG["primary"]["high_bin_min_price"]
    out = []
    for r in rows:
        p, y = r["price"], labels[r["ticker"]]["y"]
        if p <= lo:
            out.append((r["event_ticker"], p - y))
        elif p >= hi:
            out.append((r["event_ticker"], y - p))
    return out


def cluster_bootstrap(
    pairs: list[tuple[str, float]], stat: Any, resamples: int
) -> tuple[float, float]:
    by_event: dict[str, list[float]] = defaultdict(list)
    for ev, v in pairs:
        by_event[ev].append(v)
    events = sorted(by_event)
    rng = random.Random(SEED)  # noqa: S311 (statistical resampling, not security)
    stats = []
    for _ in range(resamples):
        vals: list[float] = []
        for ev in rng.choices(events, k=len(events)):
            vals += by_event[ev]
        stats.append(stat(vals))
    stats.sort()
    a = (1 - CONFIG["primary"]["bootstrap"]["ci"]) / 2
    return stats[int(a * resamples)], stats[int((1 - a) * resamples) - 1]


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def fee(r: dict[str, Any]) -> float:
    """UNVERIFIED sensitivity fee per contract (see REGISTRATION.md)."""
    if r["fee_waived"]:
        return 0.0
    mult = 1.0 if r["fee_multiplier"] is None else r["fee_multiplier"]
    return mult * 0.07 * r["price"] * (1 - r["price"])


def logit(p: float) -> float:
    lo, hi = CONFIG["price"]["clip"]
    p = min(max(p, lo), hi)
    return math.log(p / (1 - p))


def recal(p: float, a: float) -> float:
    return 1 / (1 + math.exp(-a * logit(p)))


def brier(pairs: list[tuple[float, int]], a: float | None) -> float:
    return mean([((p if a is None else recal(p, a)) - y) ** 2 for p, y in pairs])


def fit_a(pairs: list[tuple[float, int]]) -> float:
    lo, hi = 0.25, 4.0
    g = (math.sqrt(5) - 1) / 2
    for _ in range(100):
        c, d = hi - g * (hi - lo), lo + g * (hi - lo)
        if brier(pairs, c) < brier(pairs, d):
            hi = d
        else:
            lo = c
    return (lo + hi) / 2


def analyze(data: dict[str, Any]) -> dict[str, Any]:
    rows, labels = data["rows"], data["labels"]
    if not rows:
        # The registered population produced no market, so no metric exists to decide on.
        return {
            "study": CONFIG["study"],
            "verdict": "INSUFFICIENT EVIDENCE",
            "blocked": "registered population rules produced zero analysable markets",
            "hypothesis_status": "UNDETERMINED",
            "fees_verified": False,
            "sample": {k: data[k] for k in data if k not in ("rows", "labels")},
        }
    sp = split(rows, labels)
    check_no_leakage(rows, labels, sp)
    B = CONFIG["primary"]["bootstrap"]["resamples"]
    lo_p, hi_p = CONFIG["primary"]["low_bin_max_price"], CONFIG["primary"]["high_bin_min_price"]

    terms = primary_terms(rows, labels)
    pooled_rows = [r for r in rows if r["price"] <= lo_p or r["price"] >= hi_p]
    n_ev = len({ev for ev, _ in terms})
    G = mean([v for _, v in terms]) if terms else float("nan")
    G_ci = cluster_bootstrap(terms, mean, B) if terms else (float("nan"), float("nan"))
    fees = [fee(r) for r in pooled_rows]
    mue = (
        mean([f + CONFIG["economics"]["execution_allowance_dollars"] for f in fees])
        if fees
        else float("nan")
    )
    net = [(r["event_ticker"], v - fee(r)) for r, (_, v) in zip(pooled_rows, terms, strict=True)]

    bins = {}
    for name, cond in (("low", lambda p: p <= lo_p), ("high", lambda p: p >= hi_p)):
        rs = [r for r in rows if cond(r["price"])]
        if rs:
            gap = [(r["event_ticker"], labels[r["ticker"]]["y"] - r["price"]) for r in rs]
            bins[name] = {
                "markets": len(rs),
                "events": len({r["event_ticker"] for r in rs}),
                "mean_price": mean([r["price"] for r in rs]),
                "yes_rate": mean([labels[r["ticker"]]["y"] for r in rs]),
                "yes_rate_minus_price": mean([v for _, v in gap]),
                "ci": cluster_bootstrap(gap, mean, B),
            }

    ev_of = {r["ticker"]: r["event_ticker"] for r in rows}
    tr = [
        (r["price"], labels[r["ticker"]]["y"], r["ticker"])
        for r in rows
        if r["event_ticker"] in set(sp["train"])
    ]
    te = [
        (r["price"], labels[r["ticker"]]["y"], r["ticker"])
        for r in rows
        if r["event_ticker"] in set(sp["test"])
    ]
    secondary: dict[str, Any] = {
        "train_markets": len(tr),
        "test_markets": len(te),
        "train_events": len(sp["train"]),
        "test_events": len(sp["test"]),
        "purged_events": len(sp["purged"]),
        "split_S": sp["S"],
    }
    if tr and te:
        a = fit_a([(p, y) for p, y, _ in tr])
        diffs = [(ev_of[t], (p - y) ** 2 - (recal(p, a) - y) ** 2) for p, y, t in te]
        secondary |= {
            "a": a,
            "brier_raw": brier([(p, y) for p, y, _ in te], None),
            "brier_candidate": brier([(p, y) for p, y, _ in te], a),
            "raw_minus_candidate": mean([d for _, d in diffs]),
            "raw_minus_candidate_ci": cluster_bootstrap(diffs, mean, B),
        }

    fees_verified = False  # fee schedule PDF unreachable on 2026-09-27 (REGISTRATION.md)
    min_ev = CONFIG["decision_rule"]["min_events_in_pooled_bins"]
    if (
        n_ev >= min_ev
        and G_ci[0] > mue
        and fees_verified
        and secondary.get("brier_candidate", 1) < secondary.get("brier_raw", 0)
    ):
        verdict = "PROMOTE TO NEXT STAGE"
    elif n_ev >= min_ev and G_ci[1] < mue:
        verdict = "REJECT"
    else:
        verdict = "INSUFFICIENT EVIDENCE"
    hypothesis = (
        "REFUTED" if G_ci[1] < 0 else "SUPPORTED (HYPOTHESIS)" if G_ci[0] > 0 else "UNDETERMINED"
    )
    ages = sorted(r["decision_ts"] - r["price_ts"] for r in rows)
    return {
        "study": CONFIG["study"],
        "verdict": verdict,
        "hypothesis_status": hypothesis,
        "fees_verified": fees_verified,
        "sample": {
            k: data[k]
            for k in (
                "eligible_markets",
                "eligible_events",
                "sampled_markets",
                "feature_reasons",
                "sampling_stopped",
                "validation",
                "exclusions",
            )
        }
        | {
            "listing": data["listing"],
            "analysed_markets": len(rows),
            "analysed_events": len({r["event_ticker"] for r in rows}),
            "median_price_age_s": ages[len(ages) // 2] if ages else None,
        },
        "primary": {
            "G": G,
            "ci": G_ci,
            "pooled_markets": len(terms),
            "pooled_events": n_ev,
            "mue_UNVERIFIED_fees": mue,
            "bins": bins,
        },
        "economics": {
            "mean_fee_per_contract_UNVERIFIED": mean(fees) if fees else None,
            "ev_after_fee_per_contract": mean([v for _, v in net]) if net else None,
            "ev_after_fee_ci": cluster_bootstrap(net, mean, B) if net else None,
            "ev_after_fee_and_allowance": (G - mue) if terms else None,
            "note": "upper bound: last trade is not a fill; spread, depth, queue unknown",
        },
        "secondary": secondary,
    }


def fmt(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:.4f}"
    if isinstance(x, (list, tuple)):
        return "[" + ", ".join(fmt(v) for v in x) + "]"
    return str(x)


def write_results(res: dict[str, Any]) -> None:
    (HERE / "results.json").write_text(json.dumps(res, indent=2, sort_keys=True) + "\n")
    if "blocked" in res:
        (HERE / "RESULTS.md").write_text(
            "# Study 0001 results\n\n"
            "Generated by `just study-0001` from the saved snapshot; do not edit by hand.\n\n"
            f"Reproducibility: {REPRODUCIBILITY}\n\n"
            f"Verdict: **{res['verdict']}**. Blocked: {res['blocked']}.\n\n"
            "```json\n" + json.dumps(res["sample"], indent=2, sort_keys=True) + "\n```\n"
        )
        return
    p, s, e, sm = res["primary"], res["secondary"], res["economics"], res["sample"]
    lines = [
        "# Study 0001 results",
        "",
        "Generated by `just study-0001` from the saved snapshot; do not edit by hand.",
        "",
        f"Reproducibility: {REPRODUCIBILITY}",
        "",
        f"Verdict: **{res['verdict']}** (fees verified: {res['fees_verified']}). "
        f"Hypothesis on this sample: {res['hypothesis_status']}.",
        "",
        f"Sample: {sm['analysed_markets']} markets on {sm['analysed_events']} events "
        f"analysed; {res['sample']['eligible_markets']} eligible markets on "
        f"{res['sample']['eligible_events']} events; {res['sample']['sampled_markets']} sampled.",
        "",
        "| Quantity | Value |",
        "|---|---|",
        f"| Pooled edge G (per contract, gross) | {fmt(p['G'])} |",
        f"| G 95% event-clustered CI | {fmt(p['ci'])} |",
        f"| Pooled markets / events | {p['pooled_markets']} / {p['pooled_events']} |",
        f"| MUE (UNVERIFIED fee + 0.01) | {fmt(p['mue_UNVERIFIED_fees'])} |",
        f"| Mean fee per contract (UNVERIFIED) | {fmt(e['mean_fee_per_contract_UNVERIFIED'])} |",
        f"| EV after fee per contract, CI | {fmt(e['ev_after_fee_per_contract'])}, "
        f"{fmt(e['ev_after_fee_ci'])} |",
    ]
    for name, b in p["bins"].items():
        lines.append(
            f"| {name} bin: markets/events, mean price, YES rate, gap [CI] | "
            f"{b['markets']}/{b['events']}, {fmt(b['mean_price'])}, {fmt(b['yes_rate'])}, "
            f"{fmt(b['yes_rate_minus_price'])} {fmt(b['ci'])} |"
        )
    for k in (
        "train_events",
        "test_events",
        "purged_events",
        "a",
        "brier_raw",
        "brier_candidate",
        "raw_minus_candidate",
        "raw_minus_candidate_ci",
    ):
        if k in s:
            lines.append(f"| secondary {k} | {fmt(s[k])} |")
    lines += ["", "Economics are an upper bound: a last trade is not a guaranteed fill.", ""]
    (HERE / "RESULTS.md").write_text("\n".join(lines))


if __name__ == "__main__":
    raw = Path(sys.argv[1]) if len(sys.argv) > 1 else RAW
    require_snapshot(raw)
    write_results(analyze(build(raw)))
