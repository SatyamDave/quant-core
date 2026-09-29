# Runbook: venue API key rotation

> Status 2026-09-27: planned procedure. No venue adapter, venue key or live process exists, so
> none of these steps can be exercised yet. Owner: the operator. Blocked until a live broker adapter exists.

Applies to venue API keys (root rule 9: trade-only, withdrawal disabled, IP-allowlisted, scoped
per strategy and per environment). Rotate on a schedule, on suspected compromise, or when a
strategy or environment is retired.

## Steps

1. **Confirm the new key's scope before creating it:** trade-only (withdrawal permission off),
   IP-allowlisted to the trading host(s) that will use it, and scoped to one strategy and one
   environment (`config/environments/{dev,paper,canary,prod}`) — never a shared key across
   strategies or across environments.
2. **Create the new key** on the venue directly. Never generate or store it in this repo, in an
   issue, in a PR, or in a chat with an agent — root rule 2. `TODO (ops phase): secret manager is
   where it's actually stored; until that's wired up, follow your team's current manual secret
   handling and do not put it in `.env` on a shared or trading host without confirming it's
   gitignored (it is, by default, but confirm before assuming).`
3. **Deploy the new key to the trading host's secret manager entry** for that strategy and
   environment. Do not restart with both old and new key active longer than necessary to confirm
   the new one works.
4. **Confirm the new key works** with a read-only call first (e.g. account/balance query), then a
   minimal-size canary-environment order if the venue and gate allow it — never validate a new
   production key with a production-size order.
5. **Revoke the old key** on the venue once the new one is confirmed live. An old key left active
   "just in case" is a live credential nobody is watching.
6. **Record the rotation**: which key (by venue-assigned id, never the secret itself), which
   strategy/environment, who did it, when, and why (scheduled / suspected compromise / retirement)
   — in the append-only compliance/audit trail location your team uses
   (`TODO (fund phase): exact location`).

## If this is a suspected compromise, not a scheduled rotation

Treat as an incident (`.github/ISSUE_TEMPLATE/incident.yml`): halt trading on that key immediately
(don't wait for the new key to be ready — see `kill-switch.md`), revoke first, then rotate.
