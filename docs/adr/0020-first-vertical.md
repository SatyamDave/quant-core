# ADR-0020: First vertical

## Status

Proposed. Date proposed: 2026-09-27. Not yet approved by a human.

This ADR conflicts with the bootstrap's initial arena. The bootstrap spec (§1) sets the initial arena as liquid crypto perpetuals and spot, and ADR-0003 (venue selection, Proposed, [PR #5](https://github.com/OWNER/quant-core/pull/5)) reviews venues inside that arena. ADR-0003 selects no venue until the operators confirm their jurisdiction. If they are US persons, its shortlist is Coinbase Derivatives (US perpetual-style futures), Kraken Derivatives US (perps on Bitnomial through NinjaTrader Clearing) and CME crypto futures through an FCM, plus spot on Coinbase Exchange, Kraken and Binance.US. It does not consider event contracts. The base-complete spec proposes kalshi-events instead. A human must pick one:

- If kalshi-events is approved, this ADR supersedes the arena part of ADR-0003 (its Context and its shortlist, not its eligibility research or selection criteria) and of `docs/VISION.md`. Both stay in place, marked as partly superseded.
- If crypto perps are chosen, this ADR is marked Deprecated and ADR-0003 governs the venue choice. Candidate C below is one entry on ADR-0003's US shortlist; Kraken Derivatives US is on that shortlist too but is not scored here.

## Context

The base-complete scope rule is "General interfaces, one implementation": the core supports any market family, but only one vertical is built end to end until it has a live, reconciled track record. This ADR picks that vertical.

Every factual claim below cites a source and was checked on 2026-09-27. Claims that could not be confirmed from a primary source are marked UNVERIFIED and repeated in the question list at the end. The operators' jurisdiction is not confirmed, so every legal-access conclusion is marked REQUIRES HUMAN CONFIRMATION. This ADR makes no performance claims and sets no return targets.

### Candidate A: kalshi-events (Kalshi event contracts)

Regulation and access.

- KalshiEX LLC was designated a contract market (DCM) by the CFTC; the press release is dated 2020-11-04. [CFTC release 8302-20](https://www.cftc.gov/PressRoom/PressReleases/8302-20), [designation order](https://www.cftc.gov/sites/default/files/filings/documents/2020/orgkexkalshidesignation201103.pdf).
- Kalshi's help center says trading from outside the US is possible from "many countries", subject to the Member Agreement, and lists no US state restrictions. [Kalshi help: trading outside the US](https://help.kalshi.com/en/articles/14026044-can-i-trade-on-kalshi-from-outside-the-united-states).
- State access is contested and changing. CoinDesk reported on 2026-08-21 that Kalshi is off limits to customers in Washington, Michigan and Nevada, with legal disputes in Massachusetts, Minnesota, Ohio, Maryland, Utah, Arizona and New York, mostly about sports contracts. [CoinDesk, 2026-08-21](https://www.coindesk.com/news-analysis/2026/08/21/kalshi-off-limits-in-multiple-states-as-prediction-markets-cftc-team-up-for-battle). This is a news source, not a Kalshi or regulator statement: the current state list is UNVERIFIED.
- Conclusion: a US person in a state where Kalshi is available can open an account. REQUIRES HUMAN CONFIRMATION of the operators' state, the trading entity, and whether that entity is eligible under the Member Agreement.

APIs (from [docs.kalshi.com](https://docs.kalshi.com/llms.txt)).

- REST: production `https://external-api.kalshi.com/trade-api/v2`, demo `https://external-api.demo.kalshi.co/trade-api/v2`. WebSocket: production `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, demo `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`. Demo keys work only against demo and production keys only against production. [API environments](https://docs.kalshi.com/getting_started/api_environments.md).
- Demo web app: `https://demo.kalshi.co/`, with mock funds; Kalshi warns that demo prices and behavior "may not be reflective of those in real markets". [Demo environment](https://docs.kalshi.com/getting_started/demo_env.md).
- FIX: FIXT.1.1 / FIX 5.0 SP2 over TLS 1.2+. Demo hosts `fix.demo.kalshi.co` and `marketdata.fix.demo.kalshi.co`; production `mm.fix.elections.kalshi.com` and `marketdata.fix.elections.kalshi.com`. Order entry without retransmission (port 8228), market data (8233) and RFQ (8232) are listed without a stated gate; drop copy (8229), post trade (8231) and order entry with retransmission (8230) are gated behind `institutional@kalshi.com`. AWS PrivateLink needs Premier tier or above; VPC peering is discussed at Prime or above. [FIX connectivity](https://docs.kalshi.com/fix/connectivity.md). Whether any FIX session needs a signed agreement or market-maker status is UNVERIFIED.
- Specs are downloadable. Fetched 2026-09-27:

  | File | `info.version` | SHA-256 |
  |---|---|---|
  | [openapi.yaml](https://docs.kalshi.com/openapi.yaml) | 3.31.0 | `1e0a942c4fb39ea48dac12c8fde51fd9a9ce81f4f43a91637529be2c5624ed02` |
  | [asyncapi.yaml](https://docs.kalshi.com/asyncapi.yaml) (AsyncAPI 3.0.0) | 2.0.0 | `fd93f44bc06f6cb92e9abefa64beb5a3aae9029e5ec17eaf44c0bc9f8cf4bf0f` |
  | [perps_openapi.yaml](https://docs.kalshi.com/perps_openapi.yaml) | 0.0.1 | `e6d79e2229cd14fcf9b3b364baee3958de41daaf365f26fe0276453476a72551` |
  | [perps_asyncapi.yaml](https://docs.kalshi.com/perps_asyncapi.yaml) | 2.0.0 | `e6c1d081b797d31be2ee25696a40e9ca6ae0168c7b428b3e86f95d154382f9b9` |

  The OpenAPI file describes itself as a "Manually defined OpenAPI spec for endpoints being migrated to spec-first approach" and has 100 paths, including orders, order book, queue position and historical endpoints. Whether it covers every endpoint the adapter needs is UNVERIFIED.
- Auth: each request carries `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (milliseconds) and `KALSHI-ACCESS-SIGNATURE`, a base64 signature over timestamp + HTTP method + path without query parameters. Keys are RSA-2048 (RSA-PSS, SHA-256, MGF1 SHA-256, salt length equal to digest length) or Ed25519. [API keys](https://docs.kalshi.com/getting_started/api_keys.md).
- Rate limits: token buckets with separate read and write budgets; most endpoints cost 10 tokens. Budgets per second range from Basic (200 read / 100 write) to Prestige (12,000 / 9,600). Basic comes with signup, Advanced by calling the upgrade endpoint, and higher tiers from trailing 30-day volume share or Kalshi's assignment. Throttled requests get HTTP 429 with no penalty. [Rate limits](https://docs.kalshi.com/getting_started/rate_limits.md). The changelog lists a 20% limit increase for Premier through Prestige in September 2026. [Changelog](https://docs.kalshi.com/changelog/index.md).
- Order book: the WebSocket book carries bids only, `yes_dollars_fp` and `no_dollars_fp` as [price, count] pairs, with a `seq` field for snapshot/delta consistency. [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates.md). The subscription accepts a `get_snapshot` action that returns a fresh snapshot without changing the subscription (asyncapi.yaml above).
- Fees: each series carries a `fee_type` (`quadratic`, `quadratic_with_maker_fees`, `quadratic_with_combo_maker_fees`, `flat`) and a `fee_multiplier`, defined by reference to the [fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf) (openapi.yaml above). Per-fill fees round up to $0.000001, with rounding overpayment rebated across fills of one order. [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding.md). The PDF returned HTTP 429 on every attempt, so the coefficients (secondary sources quote 0.07 × P × (1 − P) for takers) are UNVERIFIED.
- Market maker program: Kalshi grants status after reviewing financial resources, trading experience and reputation, in return for quoting obligations, reduced fees and adjusted position limits. [Kalshi help: become a market maker](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi), [2025 program terms filed with the CFTC](https://www.cftc.gov/sites/default/files/filings/orgrules/25/04/rules04212519494.pdf). Current eligibility thresholds are UNVERIFIED.
- Historical data: `/historical/markets`, candlesticks, trades, fills, orders and positions, split from live endpoints at cutoffs returned by `GET /historical/cutoff`, with cursor pagination. No bulk download is documented. [Historical data](https://docs.kalshi.com/getting_started/historical_data.md). Whether historical order-book depth is available at all is UNVERIFIED; plan on recording our own.

Stack fit. ADR-0001 (Proposed) makes NautilusTrader the engine foundation and source of live venue adapters, and limits hftbacktest to fill simulation behind our own `backtest/fill_models/` interface. NautilusTrader has no Kalshi adapter: RFC [#4780](https://github.com/nautechsystems/nautilus_trader/issues/4780) is open and PR [#5010](https://github.com/nautechsystems/nautilus_trader/pull/5010) (Kalshi adapter plus prediction-market core types) was closed unmerged on 2026-09-17. It does ship a Polymarket adapter (`crates/adapters/polymarket` on `develop`). hftbacktest's live support is Binance Futures and Bybit only ([README](https://github.com/nkaz001/hftbacktest)), and it has no binary-contract settlement model. So under ADR-0001 the Kalshi gateway is ours, generated from the pinned specs behind the VenueAdapter interface, and the event-family fill and settlement models are ours too; hftbacktest's queue model is at most a starting point for the binary-book fill model.

### Candidate B: Kalshi perpetual futures

- The CFTC approved KalshiEX's BTCPERP perpetual contract on 2026-05-29. [CFTC release 9240-26](https://www.cftc.gov/PressRoom/PressReleases/9240-26). Kalshi announced perps with a waitlist signup. [Kalshi news](https://news.kalshi.com/p/kalshi-launches-perpetual-futures-america).
- The perps API has REST under `/margin`, WebSocket and FIX, in demo and production; production is "rolling out member-by-member". [Perps API](https://docs.kalshi.com/margin.md). Access criteria, and whether the operators can get in, are UNVERIFIED.
- Same auth and key model as event contracts, separate specs (above). Short history: the first contract was approved four months ago.

### Candidate C: crypto perps on Coinbase Derivatives Exchange (CDE)

- CFTC Letter 26-19 (2026-06-12) addresses CDE's request to remove expiry dates from its previously self-certified long-dated perpetual-style futures. [CFTC Letter 26-19](https://www.cftc.gov/csl/26-19/download). CDE's DCM designation date, the launch date for US customers, leverage and clearing arrangements are reported in Coinbase pages that returned HTTP 403 ([Coinbase help: market access](https://help.coinbase.com/en/derivatives/perpetual-style-futures/market-access)), so they are UNVERIFIED.
- CDE offers REST, FIX 4.4, SBE and UDP multicast; SBE order entry and UDP market data in production are cross-connect only. Production runs in Equinix CH4 (Chicago), DR and integration in NY5 (Secaucus); there is an integration/UAT environment, and a firm must be certified before connecting. [CDE welcome](https://docs.cdp.coinbase.com/derivatives/introduction/welcome), [connectivity](https://docs.cdp.coinbase.com/derivatives/introduction/connectivity), [runbook](https://docs.cdp.coinbase.com/derivatives/introduction/runbook). A public REST OpenAPI file is downloadable. [cde-public-api-spec.json](https://docs.cdp.coinbase.com/derivatives/downloads/cde-public-api-spec.json).
- Direct CDE access is reported to go through an FCM, with Nodal Clear as clearing organization (search summary of the Coinbase help page above; UNVERIFIED); the retail route is Coinbase's own API. NautilusTrader's Coinbase adapter covers "USD-margined perpetual swaps on the FCM venue" and dated nano futures. [Nautilus Coinbase guide](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/integrations/coinbase.md). Whether that retail route has a sandbox is UNVERIFIED.
- Legal access: REQUIRES HUMAN CONFIRMATION.

### Candidate D: CME futures via a broker

- NautilusTrader ships Interactive Brokers and Databento adapters (`crates/adapters` on `develop`), so broker order entry and CME data both have maintained paths.
- CME's 24/7 crypto futures launch in 2026 appears in search results for [CME's press release](https://www.cmegroup.com/media-room/press-releases/2026/6/01/cme_group_announceslaunchof247cryptocurrencyfuturesandoptionstra.html), but the page timed out, so it is UNVERIFIED. Broker fees, exchange fees, data fees and margin were not researched in detail and are UNVERIFIED.
- Legal access: CME is open to US persons through a registered FCM. REQUIRES HUMAN CONFIRMATION.

### Scoring

Scores run from 1 (worst) to 5 (best). Legal access assumes a US person in a state where the venue is available; every legal score REQUIRES HUMAN CONFIRMATION. Capacity and competition are the author's judgment from product structure, not measured, and are UNVERIFIED. The totals are unweighted; weighting is a human decision.

| Criterion | A: kalshi-events | B: Kalshi perps | C: CDE crypto perps | D: CME via broker |
|---|---|---|---|---|
| Legal access | 4: DCM, self-serve signup; some states off limits | 2: waitlist, member-by-member | 4: DCM, via FCM; details UNVERIFIED | 4: via FCM |
| Data availability | 4: public REST + WS + historical API; no bulk depth history | 2: history starts mid-2026 | 3: REST/FIX public; depth history needs recording or a vendor | 4: vendor data via Databento, at a cost |
| Capacity (judgment) | 2: many thin markets | 3: UNVERIFIED | 3: UNVERIFIED | 5: deep central limit books |
| Competition (judgment) | 3: fewer latency-driven participants on thin markets, UNVERIFIED | 3: new product, UNVERIFIED | 3: UNVERIFIED | 1: mature, heavily contested |
| Stack fit | 3: gateway and event family are new code; spec-generated; demo for tests | 3: same gateway as A; orderbook family | 4: Nautilus Coinbase adapter; hftbacktest fill model fits | 4: Nautilus IB + Databento adapters |
| Time to first live trade | 4: self-serve keys, demo on day one | 2: access gated | 3: certification or retail route | 2: broker account, data subscriptions, larger contract margin (UNVERIFIED) |
| Unweighted total | 20 | 15 | 20 | 20 |

A, C and D tie on the unweighted total, and D's total leans on the capacity judgment. The deciding factors are the base-complete spec's own priorities: test without real money against an official demo, build the adapter from pinned official specs, and seek an edge from forecasting and market-making discipline rather than latency. Only A combines a self-serve demo (REST and WebSocket; FIX demo hosts exist, access terms UNVERIFIED) with open production access today; B has a demo but gated production, and C's integration environment requires certification.

## Decision

Proposed: VERTICAL = kalshi-events, conditional on the human confirming legal access.

Checkable consequences in the repository:

- The only vertical code is `engine/crates/gateway/kalshi/` and `families/event/`. No other venue adapter is built.
- The gateway is generated from pinned copies of the official OpenAPI and AsyncAPI files, with the SHA-256 recorded next to them; a spec update is a PR that changes the hash.
- All tests and paper trading use the demo environment. Production credentials are requested only after human approval and live in the secret manager (root rule 2), trade-only (rule 9).
- `engine/crates/gateway/kalshi/CLAUDE.md` lists each API quirk as UNVERIFIED until a demo test and the official docs confirm it.

## Alternatives

- **B: Kalshi perps.** Would reuse A's gateway and auth and fit the orderbook family, but production access is rolling out member by member and the history is short. Candidate for a second vertical by later ADR, not now.
- **C: CDE crypto perps.** Matches the bootstrap arena and has the best off-the-shelf stack fit through NautilusTrader. Not chosen by default because direct access needs certification through an FCM, and the retail route's sandbox is UNVERIFIED. It is the candidate where ADR-0001 applies most directly, since NautilusTrader's Coinbase adapter is the live adapter. If the human weights stack fit and the original VISION arena over demo-first testing, pick the crypto-perps arena and let ADR-0003 choose among its US shortlist.
- **D: CME via broker.** Most capacity, but the most contested market and the highest fixed cost for a small team; the edge sources the spec names are weakest there.

## Consequences

- Easier: adapter tests with no money at risk, a small and fully documented API surface, self-serve keys.
- Harder: the event family (binary contracts, settlement, resolution rules, a bids-only book) has no support in NautilusTrader or hftbacktest, so the fill model, settlement model and replay data are ours to build and validate. Capacity is limited per market, so risk controls must group correlated events.
- New obligations: track state-level restrictions and the Member Agreement as an operator compliance item; re-check spec hashes on every adapter change; record our own order-book data from day one, since no bulk depth history is documented.
- ADR-0003 and `docs/VISION.md` need a "partly superseded by ADR-0020" note if this is approved.
- Revisit if: the operators' state is restricted; Kalshi's API terms bar automated trading for our entity type; demo testing contradicts the docs on something the adapter depends on; or Kalshi perps become generally available.

### Community-reported Kalshi API quirks (UNVERIFIED pending demo testing)

From a third-party guide, [AgentBets: top 10 Kalshi API problems](https://agentbets.ai/guides/kalshi-api-top-10-problems/), and search summaries. Items marked "docs" are also stated in official docs cited above, but still need a demo test.

1. Signing must use the path without query parameters (docs).
2. Demo and production keys are not interchangeable (docs).
3. WebSocket connections require authentication.
4. Missed `seq` numbers mean the local book has drifted; resync from a fresh snapshot (`get_snapshot`) rather than replaying deltas.
5. The book is bids only; the yes ask is derived from the best no bid (docs state bids only; the derivation is UNVERIFIED).
6. Live and historical data are split at cutoffs and paginated by cursor (docs).
7. Legacy integer-cent price fields were removed in favor of `*_dollars` and `*_fp` fields; sub-cent ticks exist on some markets.
8. Fractional contracts are enabled and fees use a rounding accumulator (fee rounding is docs).
9. The legacy order endpoint was removed; the current create-order call requires `time_in_force` and `self_trade_prevention_type`.
10. Legacy endpoints cost more rate-limit tokens than their replacements.
11. Positions may lag fills (reported in the base-complete spec, source not found).
12. Some keepalive implementations drop the connection when ping/pong is not handled. [Keep-alive docs](https://docs.kalshi.com/websockets/connection-keep-alive.md) exist but were not read in detail.

### Open questions (UNVERIFIED items)

1. Current list of US states, and non-US countries, where Kalshi accounts or specific market categories are unavailable.
2. Whether the Member Agreement or API terms allow automated trading by our entity type.
3. Whether any FIX session requires a signed agreement or market-maker status.
4. Whether `openapi.yaml` covers every endpoint the adapter needs.
5. Current fee schedule coefficients and maker-fee series.
6. Current market-maker and liquidity-provider program thresholds.
7. Whether historical order-book depth is available from Kalshi.
8. Kalshi perps production access criteria.
9. CDE: DCM designation date, US launch date, leverage, clearing organization, retail-route sandbox.
10. CME: 24/7 crypto futures details, all-in cost through a broker.
11. Every capacity and competition score in the table.

## Review date

Before any adapter code is written: the human confirms jurisdiction and picks A or the ADR-0003 arena. Otherwise, before the first request for production Kalshi credentials, or 2026-12-31, whichever comes first.
