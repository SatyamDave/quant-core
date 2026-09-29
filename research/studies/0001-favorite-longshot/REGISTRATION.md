# Study 0001 registration: favorite-longshot bias on settled Kalshi binary markets

Registered 2026-09-27, before any settled-market outcome was fetched. The commit that adds this file and `config.json` is the pre-registration evidence; `config.json` is the frozen machine-readable copy and wins if the two disagree. Any change after outcomes are seen is a new trial, recorded as such.

Requests made before this commit (all in `data/raw/study-0001/requests.jsonl`, none returns a settled outcome): `GET /historical/cutoff`, four pages of `GET /markets?status=open` (schema and field check), one `GET /markets/trades` on an open market (sort-order check), one `GET /series` (fee types). Plus seven documentation fetches listed in the README.

## A. Hypothesis (forecasting skill)

One uncertainty: is the market-implied probability at a fixed time before expiry miscalibrated at the extremes in the direction of the favorite-longshot bias (FLB)? Contracts priced at or below 0.15 would resolve YES less often than their price, and contracts at or above 0.85 more often.

- Mechanism (HYPOTHESIS): some buyers prefer lottery-like payoffs and pay up for cheap YES contracts; the other side needs capital locked until settlement to sell them.
- Why it might persist (HYPOTHESIS): selling a 0.05 longshot ties up 0.95 per contract to earn 0.05 at most; the quadratic taker fee is a larger fraction of the price near the extremes; many Kalshi markets are small and short lived.
- Why competitors may not have removed it (HYPOTHESIS): capacity per market is small, so the correction is not worth a large desk's time, and the correction needs capital held to expiry.
- Falsifying observation: the pooled edge (defined below) has a 95% CI upper bound below 0, or below the minimum useful effect, on at least 100 independent events.

This studies forecasting skill only (is the price miscalibrated). It says nothing about execution quality, and the economics below are an upper bound.

## B. Design

Data source. Kalshi public REST, `https://external-api.kalshi.com/trade-api/v2`, OpenAPI 3.31.0 (sha256 `1e0a942c...24ed02`, same as ADR-0020). `/markets`, `/markets/trades`, `/historical/trades`, `/historical/cutoff` and `/series` declare no `security` in that spec, and the probes above returned 200 without credentials (VERIFIED, `requests.jsonl`).

Population. Markets returned by `GET /markets?status=settled&mve_filter=exclude`, listed one UTC day at a time with `min_settled_ts`/`max_settled_ts` over settlement dates 2026-08-01 to 2026-09-19 inclusive. That window starts after the current `market_settled_ts` cutoff (2026-07-29) so every market is on the live endpoint, and ends a week before today so settlements are final. At most 12 pages of 1000 per day and 700 listing requests in total; a day that hits its page cap is dropped whole and counted, rather than kept in an unknown API order. Kept: `market_type == binary`, `result` in {yes, no}, empty `mve_collection_ticker`, `volume_fp > 0`, `expected_expiration_time` present, `open_time < decision_ts < close_time`. Every exclusion is counted by reason.

Decision time. `decision_ts = expected_expiration_time - 24 h`. Not `close_time`: on the probe, every open market had `can_close_early = true`, and a settled market's `close_time` can move to when the outcome happened, which would make the decision time depend on the label. `expected_expiration_time` is the scheduled expiry; whether Kalshi ever revises it after listing is UNKNOWN and is a stated limitation. 24 h is long enough that the price is a forecast, not a near-settled quote, and short enough that most daily and event markets are open. Markets that closed before their decision time are excluded and counted by result, so the selection effect is visible.

Price. The last trade in `[decision_ts - 24 h, decision_ts]`: `yes_price_dollars` of the latest `created_time <= decision_ts` from `GET /markets/trades?ticker=...&min_ts=...&max_ts=decision_ts&limit=1000` (newest first, VERIFIED on one probe; the code re-checks order). If `decision_ts` is before the `trades_created_ts` cutoff, `/historical/trades` is used for the part before the cutoff. No trade in the window, or less than 10 contracts traded in it, means the market is excluded and counted; nothing is imputed. Prices are clipped to [0.01, 0.99] for the logit only.

Label. `result` (yes = 1, no = 0). Labels are read only in the evaluation step, never into the feature row.

Sample. Eligible markets are grouped by `event_ticker`. Events are ordered by sha256(`seed:event_ticker`), at most 10 markets per event by sha256(`seed:ticker`), and taken in that order until the request budget is used (keeping 20 in reserve). Cutting events whole keeps clusters intact.

Independence unit. `event_ticker`. Contracts on one event (brackets, player props) are not independent. All CIs resample events.

Baseline. The market-implied probability, `p = price`. For economics, the no-trade policy (EV 0).

Primary metric. Pooled edge `G = mean over markets in the low bin (p <= 0.15) and high bin (p >= 0.85) of s * (y - p)`, `s = -1` in the low bin and `+1` in the high bin. `G` is the gross dollar P&L per contract of the obvious trade (buy NO at `1 - p` on longshots, buy YES at `p` on favorites), before fees. FLB predicts `G > 0`. Also reported per bin: realized YES rate minus mean implied probability. 95% percentile CI from 2000 event-clustered bootstrap resamples, seed 20260927. The primary metric fits nothing, so it uses the whole sample.

Secondary (the one candidate). `p' = sigmoid(a * logit(p))`, one parameter `a` (`a > 1` means extremes are underconfident, which is FLB). `a` is fit on the train split by minimizing mean Brier (golden section on [0.25, 4], 100 iterations). Ablation: raw `p`. Metric: held-out mean Brier of raw minus candidate, event-clustered bootstrap CI. Split point `S` = the 0.6 quantile of `decision_ts` over the sample. Train: events whose every market has `settlement_ts < S`. Test: events whose every market has `decision_ts >= S`. Events in neither are purged and counted. So every train label was public before any test decision.

Minimum useful effect (MUE). Kalshi's fee schedule PDF (`https://kalshi.com/docs/kalshi-fee-schedule.pdf`) returned a Vercel security checkpoint (HTTP 429, HTML) on 2026-09-27, so the coefficients are UNVERIFIED. As sensitivity only: taker fee per contract `= fee_multiplier * 0.07 * p * (1 - p)` (the coefficient secondary sources quote, `fee_multiplier` from `GET /series`, VERIFIED to be 1 for most series), zero while `fee_waiver_expiration_time > decision_ts`. Plus 0.01 per contract execution allowance, because a last trade is not a guaranteed fill and spread and depth are unknown. `MUE = mean over pooled-bin markets of (fee + 0.01)`. At p = 0.10 that is 0.0063 + 0.01 = 0.0163.

Search budget. Exactly one primary test and one candidate with its ablation. No other bins, horizons, filters or models are evaluated on this data.

Compute limit. At most 3000 HTTP requests including retries and documentation, paced at 2 requests per second (a tenth of the Basic read budget); offline analysis on one laptop core.

Decision rule (frozen).

- PROMOTE TO NEXT STAGE (forward shadow observation only): at least 100 events in the pooled bins, CI lower bound of `G` above MUE, fee schedule verified from an official Kalshi document, held-out Brier of the candidate below raw, and every validation and leakage check passing. With the fee document unverified, PROMOTE is not reachable in this run.
- REJECT: at least 100 events in the pooled bins and CI upper bound of `G` below MUE.
- INSUFFICIENT EVIDENCE: anything else.

Separately from tradeability, the hypothesis is tagged REFUTED on this sample if the CI upper bound of `G` is below 0, and supported (HYPOTHESIS still, not VERIFIED) if the lower bound is above 0.
