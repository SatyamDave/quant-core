"""Fetch a study 0002 snapshot into data/raw/study-0002, then compare it with the catalog.

Resumable: files already on disk are not re-requested. Stops cleanly when the budget is spent.
Spends real (unauthenticated, public) Kalshi requests, up to the registered budget.

Reuses research/studies/0001-favorite-longshot/kalshi_client.py unmodified (imported, not
copied): the budgeted GET client, its request ledger and its retry/backoff behaviour are exactly
study 0001's. Only MAX_REQUESTS is overridden here to this study's own (smaller) budget; study
0001's file and its own ledger are untouched.

The committed catalog data/catalog/study-0002.json records this run's snapshot and is never
overwritten here. The new snapshot's catalog goes to out/study-0002/catalog.json, and the files
are checked against the committed hashes: any difference means a different snapshot, which is a
new trial needing its own registration, not a reproduction of this run.
"""

import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import study

STUDY_0001_DIR = Path(__file__).resolve().parents[1] / "0001-favorite-longshot"
sys.path.insert(0, str(STUDY_0001_DIR))
import kalshi_client  # noqa: E402  (attribution: research/studies/0001-favorite-longshot/kalshi_client.py, unmodified)
from kalshi_client import BudgetExhausted, Client  # noqa: E402

kalshi_client.MAX_REQUESTS = study.CONFIG["request_budget_total"]

RESERVE = 20


def list_markets(c: Client) -> None:
    """One /markets?series_ticker=...&status=settled listing per configured series (not per day:
    each series' settled volume over the window is small enough that one series fits well under
    max_pages_per_series, unlike study 0001's whole-exchange per-day listing)."""
    lst = c.raw_dir / "listing"
    if (lst / "manifest.json").exists():
        return
    cfg = study.CONFIG["listing"]
    start, end = (
        datetime.fromisoformat(x.replace("Z", "+00:00")) for x in cfg["window_settled_utc"]
    )
    done, dropped, stop_reason = [], [], None
    for series_ticker in cfg["series_tickers"]:
        pages, cursor = [], None
        for _ in range(cfg["max_pages_per_series"]):
            if c.used >= study.CONFIG["request_budget_listing_max"]:
                stop_reason = "listing request cap reached"
                break
            params = dict(
                cfg["params"],
                series_ticker=series_ticker,
                min_settled_ts=int(start.timestamp()),
                max_settled_ts=int(end.timestamp()),
                cursor=cursor,
            )
            r = c.get("/markets", params, f"listing/tmp_{series_ticker}_{len(pages)}.json")
            pages.append(r["markets"])
            cursor = r.get("cursor")
            if not cursor:
                break
        if stop_reason:
            break
        if cursor:
            # Hit max_pages_per_series without exhausting the cursor: sizing under-counted this
            # series. Drop it whole rather than keep an unknown-order partial page, same rule as
            # study 0001, but this should not happen given the events-based sizing in
            # REGISTRATION.md.
            dropped.append(series_ticker)
        else:
            merged = [m for p in pages for m in p]
            (lst / f"series_{series_ticker}.json").write_text(json.dumps({"markets": merged}))
            done.append(series_ticker)
        for f in lst.glob(f"tmp_{series_ticker}_*.json"):
            f.unlink()
    for f in lst.glob("tmp_*.json"):
        f.unlink()
    manifest = {
        "series_complete": done,
        "series_dropped_page_cap": dropped,
        "stop_reason": stop_reason,
        "listing_requests_through": c.used,
    }
    (lst / "manifest.json").write_text(json.dumps(manifest, indent=1))


def fetch_series_metadata(c: Client) -> None:
    out = c.raw_dir / "series"
    if (out / "series.json").exists():
        return
    series = []
    for ticker in study.CONFIG["listing"]["series_tickers"]:
        r = c.get("/series/" + ticker, {}, f"series/tmp_{ticker}.json")
        series.append(r["series"])
    out.mkdir(exist_ok=True)
    (out / "series.json").write_text(json.dumps({"series": series}, indent=1))
    for f in out.glob("tmp_*.json"):
        f.unlink()


def fetch_trades(c: Client, cutoff: int) -> None:
    markets, _ = study.load_listing(c.raw_dir)
    kept, _ = study.eligible(markets)
    done, stopped = [], "sample exhausted"
    for m in study.sample_order(kept):
        wins = study.trade_windows(m, cutoff)
        files = [study.trade_file(m["ticker"], ep) for ep, _, _ in wins]
        if not all((c.raw_dir / f).exists() for f in files):
            if c.used + len(wins) > kalshi_client.MAX_REQUESTS - RESERVE:
                stopped = "request budget reached"
                break
            try:
                for (ep, lo, hi), f in zip(wins, files, strict=True):
                    if not (c.raw_dir / f).exists():
                        c.get(
                            ep,
                            {"ticker": m["ticker"], "min_ts": lo, "max_ts": hi, "limit": 1000},
                            f,
                        )
            except BudgetExhausted:
                stopped = "request budget reached"
                break
        done.append({"ticker": m["ticker"], "files": files})
    out = {"markets": done, "stopped": stopped, "trades_created_ts_cutoff": cutoff}
    (c.raw_dir / "trades").mkdir(exist_ok=True)
    (c.raw_dir / "trades" / "sampled.json").write_text(json.dumps(out, indent=1))


def catalog(c: Client, cutoff_doc: dict[str, str]) -> None:
    files = sorted(p for p in c.raw_dir.rglob("*.json") if p.is_file())
    ledger = [json.loads(line) for line in c.ledger.read_text().splitlines()]
    outcome_fetches = [r["fetched_at"] for r in ledger if "status=settled" in r["url"]]
    doc = {
        "dataset": "study-0002",
        "location": "data/raw/study-0002 (gitignored; hashes only here)",
        "api_base": study.CONFIG["api_base"],
        "openapi_version": study.CONFIG["openapi_version"],
        "openapi_sha256": study.CONFIG["openapi_sha256"],
        "historical_cutoff_at_fetch": cutoff_doc,
        "requests_total": len(ledger),
        "requests_non_200": sum(1 for r in ledger if r["status"] != 200),
        "first_request_at": ledger[0]["fetched_at"],
        "last_request_at": ledger[-1]["fetched_at"],
        "first_settled_outcome_fetch_at": min(outcome_fetches) if outcome_fetches else None,
        "listing_params": study.CONFIG["listing"],
        "files": {
            str(p.relative_to(c.raw_dir)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        },
        "record_counts": {
            "listed_markets": sum(
                len(json.loads(p.read_text())["markets"])
                for p in (c.raw_dir / "listing").glob("series_*.json")
            ),
            "trade_files": len(list((c.raw_dir / "trades").rglob("*/*.json"))),
        },
        "ledger_sha256": hashlib.sha256(c.ledger.read_bytes()).hexdigest(),
        "catalog_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out = study.ROOT / "out" / "study-0002" / "catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")


def main() -> None:
    c = Client(study.RAW)
    cutoff_path = c.raw_dir / "cutoff.json"
    if not cutoff_path.exists():
        c.get("/historical/cutoff", {}, "cutoff.json")
    cutoff_doc = json.loads(cutoff_path.read_text())
    fetch_series_metadata(c)
    try:
        list_markets(c)
    except BudgetExhausted as e:
        sys.exit(f"stopped: {e}")
    cutoff = int(
        datetime.fromisoformat(cutoff_doc["trades_created_ts"].replace("Z", "+00:00"))
        .astimezone(UTC)
        .timestamp()
    )
    fetch_trades(c, cutoff)
    catalog(c, cutoff_doc)
    print(f"requests used: {c.used}")
    problems = study.snapshot_problems(c.raw_dir)
    if problems:
        print(
            f"DIFFERENT SNAPSHOT: {len(problems)} difference(s) from {study.CATALOG}; this is "
            "not this run and needs its own registration:\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"same snapshot as {study.CATALOG}: every sha256 matches")


if __name__ == "__main__":
    main()
