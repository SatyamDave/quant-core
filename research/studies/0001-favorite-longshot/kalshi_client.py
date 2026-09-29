"""Budgeted, unauthenticated GET client for Kalshi's public market-data endpoints.

Every request (retries included) is appended to a ledger next to the raw data, and the
ledger is the budget: the client refuses to send once it holds MAX_REQUESTS lines.
"""

import hashlib
import http.client
import json
import time
import urllib.parse
from pathlib import Path

HOST = "external-api.kalshi.com"
BASE_PATH = "/trade-api/v2"
BASE = f"https://{HOST}{BASE_PATH}"
# 3000 total minus 7 documentation fetches made by hand on 2026-09-27 (see README).
MAX_REQUESTS = 2993
# Basic read budget is 200 tokens/s at 10 tokens per request (docs rate_limits.md,
# 2026-09-27), i.e. 20 req/s. 2 req/s stays at a tenth of it.
MIN_INTERVAL_S = 0.5
MAX_RETRIES = 4


class BudgetExhausted(RuntimeError):
    pass


class Client:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir
        self.ledger = raw_dir / "requests.jsonl"
        raw_dir.mkdir(parents=True, exist_ok=True)
        self.used = len(self.ledger.read_text().splitlines()) if self.ledger.exists() else 0
        self._last = 0.0

    def _log(self, row: dict[str, object]) -> None:
        self.used += 1
        with self.ledger.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def get(self, path: str, params: dict[str, object], save_as: str) -> dict[str, object]:
        """GET BASE+path, save the body under raw_dir/save_as, return parsed JSON."""
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{BASE}{path}?{query}" if query else f"{BASE}{path}"
        target = f"{BASE_PATH}{path}?{query}" if query else f"{BASE_PATH}{path}"
        for attempt in range(MAX_RETRIES + 1):
            if self.used >= MAX_REQUESTS:
                raise BudgetExhausted(f"request budget {MAX_REQUESTS} exhausted")
            wait = self._last + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # HTTPSConnection to a fixed host: no other scheme or host is reachable from here.
            conn = http.client.HTTPSConnection(HOST, timeout=30)
            try:
                conn.request("GET", target, headers={"Accept": "application/json"})
                resp = conn.getresponse()
                status, body = resp.status, resp.read()
            except (OSError, http.client.HTTPException) as e:
                status, body = 0, str(e).encode()
            finally:
                conn.close()
            row: dict[str, object] = {
                "url": url,
                "status": status,
                "fetched_at": fetched_at,
                "attempt": attempt,
                "bytes": len(body),
            }
            if status == 200:
                out = self.raw_dir / save_as
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(body)
                row |= {"file": save_as, "sha256": hashlib.sha256(body).hexdigest()}
                self._log(row)
                parsed: dict[str, object] = json.loads(body)
                return parsed
            self._log(row | {"error": body[:300].decode(errors="replace")})
            if status in (429, 0) or status >= 500:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"HTTP {status} for {url}: {body[:300]!r}")
        raise RuntimeError(f"gave up after {MAX_RETRIES} retries: {url}")
