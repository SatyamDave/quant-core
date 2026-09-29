"""Fetch a study 0001 snapshot into data/raw/study-0001, then compare it with the catalog.

Resumable: files already on disk are not re-requested. Stops cleanly when the budget is spent.
Spends real (unauthenticated, public) Kalshi requests, up to the registered budget.

The committed catalog data/catalog/study-0001.json records run 0001's snapshot and is never
overwritten here. The new snapshot's catalog goes to out/study-0001/catalog.json, and the files
are checked against the committed hashes: any difference means a different snapshot, which is a
new trial needing its own registration, not a reproduction of run 0001.
"""

import hashlib
import json
import sys
import time
from datetime import UTC, datetime, timedelta

import study
from kalshi_client import MAX_REQUESTS, BudgetExhausted, Client

RESERVE = 20


def list_markets(c: Client) -> None:
    lst = c.raw_dir / "listing"
    if (lst / "manifest.json").exists():
        return
    cfg = study.CONFIG["listing"]
    start, end = (
        datetime.fromisoformat(x.replace("Z", "+00:00")) for x in cfg["window_settled_utc"]
    )
    days_done, days_dropped, stop_reason = [], [], None
    day = start
    while day < end:
        pages, cursor, name = [], None, day.strftime("%Y-%m-%d")
        for _ in range(cfg["max_pages_per_day"]):
            if c.used >= study.CONFIG["request_budget_listing_max"]:
                stop_reason = "listing request cap reached"
                break
            params = dict(
                cfg["params"],
                min_settled_ts=int(day.timestamp()),
                max_settled_ts=int((day + timedelta(days=1)).timestamp()),
                cursor=cursor,
            )
            r = c.get("/markets", params, f"listing/tmp_{name}_{len(pages)}.json")
            pages.append(r["markets"])
            cursor = r.get("cursor")
            if not cursor:
                break
        if stop_reason:
            break
        if cursor:
            days_dropped.append(name)
        else:
            merged = [m for p in pages for m in p]
            (lst / f"day_{name}.json").write_text(json.dumps({"markets": merged}))
            days_done.append(name)
        for f in lst.glob(f"tmp_{name}_*.json"):
            f.unlink()
        day += timedelta(days=1)
    for f in lst.glob("tmp_*.json"):
        f.unlink()
    manifest = {
        "days_complete": days_done,
        "days_dropped_page_cap": days_dropped,
        "stop_reason": stop_reason,
        "listing_requests_through": c.used,
    }
    (lst / "manifest.json").write_text(json.dumps(manifest, indent=1))


def fetch_trades(c: Client, cutoff: int) -> None:
    markets, _ = study.load_listing(c.raw_dir)
    kept, _ = study.eligible(markets)
    done, stopped = [], "sample exhausted"
    for m in study.sample_order(kept):
        wins = study.trade_windows(m, cutoff)
        files = [study.trade_file(m["ticker"], ep) for ep, _, _ in wins]
        if not all((c.raw_dir / f).exists() for f in files):
            if c.used + len(wins) > MAX_REQUESTS - RESERVE:
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
        "dataset": "study-0001",
        "location": "data/raw/study-0001 (gitignored; hashes only here)",
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
                for p in (c.raw_dir / "listing").glob("day_*.json")
            ),
            "trade_files": len(list((c.raw_dir / "trades").rglob("*/*.json"))),
        },
        "ledger_sha256": hashlib.sha256(c.ledger.read_bytes()).hexdigest(),
        "catalog_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out = study.ROOT / "out" / "study-0001" / "catalog.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")


def main() -> None:
    c = Client(study.RAW)
    cutoff_path = c.raw_dir / "cutoff.json"
    if not cutoff_path.exists():
        c.get("/historical/cutoff", {}, "cutoff.json")
    cutoff_doc = json.loads(cutoff_path.read_text())
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
            "not run 0001 and needs its own registration:\n  " + "\n  ".join(problems),
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"same snapshot as {study.CATALOG}: every sha256 matches")


if __name__ == "__main__":
    main()
