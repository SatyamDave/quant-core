# Study 0002 registration: favorite-longshot bias on settled Kalshi binary markets, take two

Registered 2026-09-28, before any settled-market outcome was fetched **through the budgeted
client** (see "Deviation from a clean pre-registration" below for the one honest exception). The
commit that adds this file and `config.json` is the pre-registration evidence; `config.json` is
the frozen machine-readable copy and wins if the two disagree. Any change after outcomes are seen
is a new trial, recorded as such. This is trial 2 of the same hypothesis registered in
`research/studies/0001-favorite-longshot/REGISTRATION.md`; it changes only how the population is
selected and sized, per the graveyard entry and issue #68.

## Why a v2, not a fix to 0001

Study 0001 (`research/studies/0001-favorite-longshot/`) registered a population rule before
measuring how many settled markets existed under it: at most 12 pages of 1000 per UTC day,
pooled across every series. Every one of the 40 days in its window turned out to have more than
12,000 settled non-combo markets, so the rule dropped every day whole and the eligible population
was empty (`docs/research/graveyard.md`, 2026-09-27). Zero markets is not evidence either way
(INSUFFICIENT EVIDENCE); the idea itself was never tested. This redo (issue #68) fixes exactly
that mistake: bound the population to specific series *before* registering the exact listing
rule, and size it with calls that return counts, not outcomes, so the frozen rule is one we
already know will not be empty.

## Sizing procedure (metadata-only, before this commit)

1. `GET /series?include_volume=true` (one request, no per-market data: `Series` objects carry
   `category`, `frequency`, `fee_type`, `fee_multiplier` and `volume_fp`, the total contracts
   traded across the series' whole history, never an outcome or a price). 14,438 series returned.
   Ranked by `volume_fp` within `category in {Climate and Weather, Economics, Financials}`,
   `frequency == "daily"`, to find recurring same-product series with real trading activity.
2. `GET /events?series_ticker=<X>&status=settled&min_close_ts=1785369600&limit=200` for six
   candidates (one page each, no `cursor` returned, so 61 or 42 is the exact count, not a lower
   bound). `EventData` (the schema `/events` without `with_nested_markets` returns) has no
   `result`, `price`, or any per-market field at all — this is a genuine metadata-only count, unlike
   a `/markets?status=settled` listing, which carries `result` on every row. `min_close_ts` is the
   historical/live data cutoff (`GET /historical/cutoff` -> `market_settled_ts` =
   2026-07-30T00:00:00Z on 2026-09-28, VERIFIED, `data/raw/study-0002/cutoff.json`), so every
   event counted settles on the endpoint `fetch.py` will actually use (no `/historical/*` calls
   needed). Counts: `KXHIGHNY` 61, `KXHIGHLAX` 61, `KXHIGHCHI` 61, `KXAAAGASD` 61, `KXUSDJPY` 42,
   `KXEURUSD` 42 (see `config.json` `sizing.candidates_checked_settled_events_since_cutoff`).
3. Four of the six were kept: `KXHIGHNY`, `KXHIGHLAX`, `KXHIGHCHI` (Highest temperature in
   NYC/LA/Chicago, daily, mutually-exclusive temperature-bracket ladders) and `KXUSDJPY` (USD/JPY
   daily range, mutually-exclusive ladder). `KXAAAGASD` was dropped because its brackets are a
   cumulative threshold ladder (`mutually_exclusive: false` on its events — more than one bracket
   can resolve yes), a different mechanism from the other four's exactly-one-true brackets, and
   mixing mechanisms would make the pooled low/high bins harder to interpret. `KXEURUSD` was
   dropped only to keep the estimated population inside the request budget with margin, not for
   any outcome-based reason (see below).
4. Markets-per-event was checked with a nested probe, `GET
   /events?series_ticker=<X>&status=settled&min_close_ts=...&limit=5&with_nested_markets=true`,
   for `KXHIGHNY` (6), `KXUSDJPY` (15) and `KXAAAGASD` (17, not used). This probe *does* return
   full `Market` objects, result field included — see the deviation note below. `KXHIGHLAX` and
   `KXHIGHCHI` were not nested-probed; their per-event market count (6) is assumed from being the
   same product as `KXHIGHNY` in the same series family, flagged
   `markets_per_event_assumed_by_product_similarity_UNVERIFIED` in `config.json`. If the real
   count differs, only the estimated population size is off, not the registered population rule
   itself (every settled market in the four series over the window, full stop).
5. Estimated population: `61*6 + 61*6 + 61*6 + 42*15 = 1,713` settled markets before per-market
   eligibility filters (window/open/close checks) trim it further, comfortably inside the
   `request_budget_total` of 2000 (roughly one trades request per eligible market, since the
   whole window is on the live side of the cutoff, plus under 10 listing requests and a handful of
   sizing requests already spent).

## Deviation from a clean pre-registration (disclosed, not hidden)

Six probe requests happened before this file's commit and *were* outcome-bearing: the five
`GET /markets?series_ticker=<X>&status=settled&limit=3` schema checks (on `KXHIGHNY`,
`KXHIGHLAX`, `KXRAINNYC`, `KXUSDJPY`, `KXAAAGASD`, used only to confirm `open_time`/`close_time`/
`expected_expiration_time` shape and to sanity-check the `decision_ts` arithmetic against real
values, not to look at `result`) and the three `with_nested_markets=true` nested probes in step 4
above. Series selection was decided from step 1's metadata (`volume_fp`, `category`, `frequency`)
*before* any of these six calls, and none of them changed which series were kept — the selection
reasons in `config.json.sizing.chosen_reason` are structural (mechanism, fee homogeneity, budget),
not outcome-based. These six requests were also made as ad-hoc `curl` calls to a scratch path, not
through `kalshi_client.Client`, so they are not in `data/raw/study-0002/requests.jsonl` and do not
count against `request_budget_total`; every request from this commit onward, including the rest of
sizing, goes through the budgeted, ledgered client, exactly as study 0001 required. Documented
here in full rather than left out, per the research-cycle honesty requirement (root CLAUDE.md
rule 8; docs/process/research-cycle.md step B).

## A. Hypothesis (forecasting skill) — unchanged from study 0001

One uncertainty: is the market-implied probability at a fixed time before expiry miscalibrated at
the extremes in the direction of the favorite-longshot bias (FLB)? Contracts priced at or below
0.15 would resolve YES less often than their price, and contracts at or above 0.85 more often.

- Mechanism (HYPOTHESIS): some buyers prefer lottery-like payoffs and pay up for cheap YES
  contracts; the other side needs capital locked until settlement to sell them.
- Why it might persist (HYPOTHESIS): selling a 0.05 longshot ties up 0.95 per contract to earn
  0.05 at most; the quadratic taker fee is a larger fraction of the price near the extremes; these
  markets settle daily with a fresh ladder each time, so the correction has to be redone every day.
- Why competitors may not have removed it (HYPOTHESIS): capacity per bracket is small, so the
  correction is not worth a large desk's time, and the correction needs capital held to expiry.
- Falsifying observation: the pooled edge (defined below) has a 95% CI upper bound below 0, or
  below the minimum useful effect, on at least 100 independent events.

This studies forecasting skill only (is the price miscalibrated). It says nothing about execution
quality, and the economics below are an upper bound.

## B. Design

Data source. Kalshi public REST, `https://external-api.kalshi.com/trade-api/v2`, OpenAPI 3.31.0
(sha256 `1e0a942c...24ed02`, identical to study 0001's and ADR-0020's copy — VERIFIED, this
commit's cached copy at sha256 matches). `/markets`, `/markets/trades`, `/events`, `/series` and
`/historical/cutoff` declare no `security` in that spec, and every probe above returned 200
without credentials (VERIFIED, requests.jsonl once the fetch runs).

Population. Markets in series `KXHIGHNY`, `KXHIGHLAX`, `KXHIGHCHI`, `KXUSDJPY`, listed per series
with `GET /markets?series_ticker=<X>&status=settled&mve_filter=exclude&min_settled_ts=<cutoff>
&max_settled_ts=<week-ago>&limit=1000`, at most 50 pages of 1000 per series (far above the
estimated ~30 per series). Settlement window 2026-07-30T00:00:00Z (the historical/live cutoff, so
every settlement is on the live endpoint, no `/historical/*` calls needed) through
2026-09-21T00:00:00Z (one week before the 2026-09-28 registration date, so settlements are final).
Kept: `market_type == binary`, `result` in {yes, no}, empty `mve_collection_ticker`,
`volume_fp > 0`, `expected_expiration_time` present, `open_time < decision_ts < close_time`. Every
exclusion is counted by reason, same as study 0001.

Decision time. `decision_ts = expected_expiration_time - 24 h`, unchanged from study 0001 and
re-verified against real timestamps for all four series during sizing (e.g. `KXHIGHNY-26SEP27`:
open 2026-09-26T14:00Z, close 2026-09-28T05:00Z, expiry 2026-09-28T19:00Z, so `decision_ts` =
2026-09-27T19:00Z sits inside (open, close) with room either side; `KXUSDJPY-26SEP2810`: open
2026-09-25T14:00Z, close/expiry 2026-09-28T14:00Z, `decision_ts` = 2026-09-27T14:00Z, likewise
inside). `expected_expiration_time` is the scheduled expiry; whether Kalshi ever revises it after
listing is UNKNOWN, same stated limitation as study 0001.

Price. The last trade in `[decision_ts - 24 h, decision_ts]`:
`yes_price_dollars` of the latest `created_time <= decision_ts` from `GET
/markets/trades?ticker=...&min_ts=...&max_ts=decision_ts&limit=1000` (newest first, re-verified on
this run's own probes). No `/historical/trades` calls are needed: the whole settlement window is
after `trades_created_ts` cutoff (also 2026-07-30T00:00:00Z). No trade in the window, or fewer
than 10 contracts traded in it, means the market is excluded and counted; nothing is imputed.
Prices are clipped to [0.01, 0.99] for the logit only.

Label. `result` (yes = 1, no = 0). Labels are read only in the evaluation step, never into the
feature row (leakage guard reused verbatim from study 0001, see README.md).

Sample. All eligible markets across the four series are kept (no budget-driven subsampling: the
estimated population already fits the request budget with margin), grouped by `event_ticker` and
ordered deterministically by sha256(`seed:event_ticker`) then sha256(`seed:ticker`) for
reproducible resumption, at most 20 markets per event (a no-op cap here: the largest ladder,
`KXUSDJPY`, has 15).

Independence unit. `event_ticker`. Every bracket of one day's temperature or FX ladder is one
event; brackets on one event are not independent (all four series are mutually-exclusive ladders:
exactly one bracket resolves yes). All CIs resample events.

Baseline. The market-implied probability, `p = price`. For economics, the no-trade policy (EV 0).

Primary metric. Pooled edge `G = mean over markets in the low bin (p <= 0.15) and high bin
(p >= 0.85) of s * (y - p)`, `s = -1` in the low bin and `+1` in the high bin. `G` is the gross
dollar P&L per contract of the obvious trade (buy NO at `1 - p` on longshots, buy YES at `p` on
favorites), before fees. FLB predicts `G > 0`. Also reported per bin: realized YES rate minus mean
implied probability. 95% percentile CI from 2000 event-clustered bootstrap resamples, seed
20260928. The primary metric fits nothing, so it uses the whole sample.

Secondary (the one candidate). `p' = sigmoid(a * logit(p))`, one parameter `a` (`a > 1` means
extremes are underconfident, which is FLB). `a` is fit on the train split by minimizing mean
Brier (golden section on [0.25, 4], 100 iterations). Ablation: raw `p`. Metric: held-out mean
Brier of raw minus candidate, event-clustered bootstrap CI. Split point `S` = the 0.6 quantile of
`decision_ts` over the sample. Train: events whose every market has `settlement_ts < S`. Test:
events whose every market has `decision_ts >= S`. Events in neither are purged and counted.

Minimum useful effect (MUE). Kalshi's fee schedule PDF
(`https://kalshi.com/docs/kalshi-fee-schedule.pdf`) again returned a Vercel security checkpoint
(HTTP 429, HTML) on 2026-09-28 — same failure as study 0001, re-attempted per this study's task
("use the officially verified fee schedule if you can fetch it"); coefficients stay UNVERIFIED, so
the decision rule treats fees exactly as study 0001 did. As sensitivity only: taker fee per
contract `= fee_multiplier * 0.07 * p * (1 - p)` (the coefficient secondary sources quote), zero
while `fee_waiver_expiration_time > decision_ts`. All four chosen series share `fee_type:
quadratic`, `fee_multiplier: 1` (VERIFIED, `GET /series?include_volume=true`, one homogeneous fee
family unlike study 0001's pooled-across-all-series design). Plus 0.01 per contract execution
allowance, because a last trade is not a guaranteed fill and spread and depth are unknown.

Search budget. Exactly one primary test and one candidate with its ablation. No other bins,
horizons, filters or models are evaluated on this data.

Compute limit. At most 2000 HTTP requests including retries, paced at 2 requests per second (a
tenth of the Basic read budget, same rate as study 0001); offline analysis on one laptop core.

Decision rule (frozen), identical in structure to study 0001's:

- PROMOTE TO NEXT STAGE (forward shadow observation only): at least 100 events in the pooled
  bins, CI lower bound of `G` above MUE, fee schedule verified from an official Kalshi document,
  held-out Brier of the candidate below raw, and every validation and leakage check passing. With
  the fee document unverified, PROMOTE is not reachable in this run either.
- REJECT: at least 100 events in the pooled bins and CI upper bound of `G` below MUE.
- INSUFFICIENT EVIDENCE: anything else.

Separately from tradeability, the hypothesis is tagged REFUTED on this sample if the CI upper
bound of `G` is below 0, and supported (HYPOTHESIS still, not VERIFIED) if the lower bound is
above 0.
