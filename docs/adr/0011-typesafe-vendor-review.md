# ADR-0011: TypeSafe AI (Jev) vendor review and access rules

## Status

Proposed. Every fact below was read from the cited page on 2026-09-27. Items marked UNVERIFIED were not found in public sources and are listed under "Questions for the human / vendor".

## Context

[ADR-0010](0010-decision-layer.md) allows a hosted decision model in the control plane behind a provider interface. The owner's description of TypeSafe AI and Jev was written from memory, so this ADR checks each claim against TypeSafe's own site, docs, legal pages and SDKs, and against the gateways that list Jev. Vendor marketing numbers are recorded as claims, not facts, and are never used to set thresholds.

### The product exists and matches the description in outline

- TypeSafe AI, Inc. publishes Jev, described as "TypeSafe's flagship model and the first System One model" ([docs.typesafe.ai](https://docs.typesafe.ai/), [typesafe.ai](https://typesafe.ai)).
- Jev "does not generate text, write code, or hold a conversation" and is not a drop-in model for coding agents ([Jev with coding agents](https://docs.typesafe.ai/introduction/coding-agents.md)).
- Weights are not offered to customers. "Jev is not fine-tuned or LoRA-adapted with customer data ... the same weights serve every account" ([Models](https://docs.typesafe.ai/models.md)). No self-hosted offering was found in the public docs.

### Verified facts

| Topic | What the source says | Source |
|---|---|---|
| Endpoint | `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <API_KEY>`. `GET /v1/models` lists models. | [API reference](https://docs.typesafe.ai/api.md), [Models](https://docs.typesafe.ai/models.md) |
| Request shape | Required fields `state` (string, object or array), `model` (string), `questions` (map of id to question). The question id "is not sent to the underlying model". | [API reference](https://docs.typesafe.ai/api.md) |
| Primitives | `noul` (yes/no, optional `criteria.true`/`criteria.false`), `choice` (required `criteria` map, at most 255 options), `score` (required ordered `criteria` array, at least two and up to 10 levels). | [API reference](https://docs.typesafe.ai/api.md) |
| Answers | Response has `model`, `answers`, `usage` (`input_tokens`, `output_tokens`). Choice answers carry `choice`, `probabilities`, `confidence`. Score answers carry `score`, `legend`, `probabilities`, `confidence`. Noul answers carry only `noul` (0 to 1); "There is no separate `confidence` value for a Noul". | [API reference](https://docs.typesafe.ai/api.md), [Noul](https://docs.typesafe.ai/primitives/noul.md) |
| Errors | 401, 422, 429 (rate limit), 529 (overloaded). Retry 429 and 529 with exponential backoff. | [API reference](https://docs.typesafe.ai/api.md) |
| Model IDs and pinning | Versioned ID `jev-1.13.0`. Aliases `jev-latest` (latest stable) and `jev-preview` (latest release, currently the same model). "An alias moves when a new release ships, so the answers behind it can change without a change on your side." The response `model` field reports the versioned ID that answered. Versioned IDs are accepted even though `GET /v1/models` currently lists only aliases. | [Models](https://docs.typesafe.ai/models.md) |
| Input limits | 64k tokens per request; 32k for `state` plus the longest question. Text only, no image, audio or video. English is the primary language. | [Models](https://docs.typesafe.ai/models.md) |
| Rate limits | 250,000 tokens per second and 1,200 requests per minute. "The limits above can change without notice." Higher limits on custom and enterprise plans. | [Models](https://docs.typesafe.ai/models.md) |
| Pricing | $0.042 per million input tokens ($42 per billion). Output tokens are free. Billing uses prepaid credits that "expire on the earlier of (y) the end of the Term and (z) the date that is 12 months after the purchase date". | [Models](https://docs.typesafe.ai/models.md), [Master Customer Agreement](https://typesafe.ai/legal/mca) §8.2(a) |
| Training on customer data | "Jev is not trained on customer requests or responses." The MCA says TypeSafe "will not, include Customer Data in a dataset used to train ... any artificial intelligence or machine learning models without Customer's prior consent." The Privacy Policy says TypeSafe "will not train or fine tune any artificial intelligence or machine learning models on your prompts or other Input". | [Models](https://docs.typesafe.ai/models.md), [MCA](https://typesafe.ai/legal/mca) §4.1, [Privacy Policy](https://typesafe.ai/legal/privacy-policy) |
| Retention | No fixed period is published. The DPA says personal data is retained "for as long as necessary taking into account the purpose of the Processing". The MCA grants TypeSafe a licence "in perpetuity" to Customer Data to derive Telemetry, to monitor fraud and abuse, and to comply with law, and Telemetry (including "hashes, summary statistics and classifications ... and learnings") may be processed "without restriction". Zero data retention is offered "for enterprise customers" only. | [DPA](https://typesafe.ai/legal/data-processing), [MCA](https://typesafe.ai/legal/mca) §4.1, §4.3, [Legal](https://docs.typesafe.ai/legal.md) |
| Region | "The Services are hosted in the United States." EU transfers use Standard Contractual Clauses. No region choice was found. | [Privacy Policy](https://typesafe.ai/legal/privacy-policy), [DPA](https://typesafe.ai/legal/data-processing) |
| SLA | No uptime commitment. TypeSafe uses "commercially reasonable efforts" for support and "does not warrant that customer's use of the services will be uninterrupted or error-free". Liability is capped at the greater of 12 months of fees and $50. | [MCA](https://typesafe.ai/legal/mca) §3, §9.3, §12.2 |
| Observed availability | The public status page showed "99.827% uptime" for `api.typesafe.ai`, with several short outages in July 2026. This is an observation, not a commitment. | [status.typesafe.ai](https://status.typesafe.ai) |
| API changes | TypeSafe may update the Services and "will use commercially reasonable efforts to provide advance notice" of changes that materially affect integration. | [MCA](https://typesafe.ai/legal/mca) §2.5 |
| Distillation ban | Customers may not "use the Services or any Output ... to perform model distillation, train a model to imitate the output of the Services, or develop (or to facilitate the development of) a similar or competing product or service". | [MCA](https://typesafe.ai/legal/mca) §2.3(b) |
| Credentials | Access only "through the mechanisms designated by TypeSafe, including an API key". Customer Users must keep credentials confidential and not share them. Reselling or sublicensing the Services is prohibited. | [MCA](https://typesafe.ai/legal/mca) §2.3(a), §2.4 |
| Official SDKs | Python `typesafe-sdk` 0.7.2 on PyPI (first public release 0.5.7 on 2026-09-14), source [typesafe-ai/typesafe-sdk-python](https://github.com/typesafe-ai/typesafe-sdk-python), MIT. JavaScript `@typesafe-ai/sdk` 0.6.0 on npm, source [typesafe-ai/typesafe-sdk-js](https://github.com/typesafe-ai/typesafe-sdk-js), MIT. The Python SDK reads `TYPESAFE_API_KEY`, defaults to base URL `https://api.typesafe.ai`, model `jev-latest`, and a 10-second timeout, and retries with backoff by default. | [pypi.org/project/typesafe-sdk](https://pypi.org/project/typesafe-sdk/), [npm](https://www.npmjs.com/package/@typesafe-ai/sdk), [Python constants](https://docs.typesafe.ai/sdk/python/api/constants.md), [Python changelog](https://docs.typesafe.ai/sdk/python/changelog.md) |
| Known weaknesses | TypeSafe documents "jagged edges" of `jev-1.13`, for example that it "does not count reliably". | [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md) |

### Gateways

| Gateway | Status | Model ID | Pin to a version? | Source |
|---|---|---|---|---|
| LiteLLM | Pass-through to `api.typesafe.ai` at `/typesafe/v1/systemone`; reads `TYPESAFE_API_KEY`. | `jev-1.13.0`, `jev-latest`, `jev-preview` | Yes, `jev-1.13.0` is listed. | [LiteLLM docs](https://docs.litellm.ai/docs/pass_through/typesafe) |
| Vercel AI Gateway | TypeSafe-compatible API at `https://ai-gateway.vercel.sh/typesafe`, billed through Vercel, BYOK supported. Its model catalogue marks the model `"zdr": "none"`, `"no_training": "all"`. It offers "evaluation fallbacks" that can rerun an uncertain question on another vendor's model. | `typesafe-ai/jev` | No versioned ID was found. | [Vercel docs](https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe), `GET https://ai-gateway.vercel.sh/v1/models` |
| OpenRouter | TypeSafe's SDK docs show base URL `https://openrouter.ai/api` and model `~typesafe/jev-latest`. The model page returned 404 when fetched. OpenRouter's model API lists a different product, `typesafe/jev-router`, which routes requests to other models and is not the System One endpoint. | `~typesafe/jev-latest` (alias) | No versioned ID was found. | [TypeSafe Python usage](https://docs.typesafe.ai/sdk/python/usage.md), `GET https://openrouter.ai/api/v1/models` |
| Cloudflare | Listed as a third-party model on Cloudflare's AI pages with "Zero data retention: Yes" and a 32,000-token context window. | `typesafe/jev` | No versioned ID was found. | [Cloudflare docs](https://developers.cloudflare.com/ai/models/typesafe/jev/) |

### Owner claims that differ from or go beyond the sources

- **"Noul: probability a statement is true".** Correct in substance. TypeSafe describes it as a yes/no question returning "the probability that the answer is yes". A Noul answer has no `confidence` field, so "calibrated probabilities with confidence" applies to Choice and Score only.
- **"~70-500 ms latency".** UNVERIFIED. The docs publish no latency figure or objective. The homepage shows a marketing comparison ("Completed in 0.114s" against "8.566s" for LLMs, "based on workflows for System One tasks"), which is a self-reported benchmark and is not used here.
- **"Early access".** Partly confirmed. The homepage links a console sign-in and contains a "Join Waitlist" element, and the docs describe creating a key in the [console](https://console.typesafe.ai/keys). Whether a new account can create a key without waiting is UNVERIFIED.
- **"Self-reported benchmarks".** Confirmed: the only performance numbers are TypeSafe's own.

## Decision

1. **Access paths.**
   - Allowed: the official API at `https://api.typesafe.ai`, called through the official `typesafe-sdk` Python package or plain HTTPS, with a key created in `console.typesafe.ai` by an account the company owns.
   - Allowed as a pass-through proxy: a LiteLLM instance we operate that forwards to `api.typesafe.ai` with a pinned versioned model ID.
   - Not allowed for production call sites today: Vercel AI Gateway, OpenRouter and Cloudflare, because none exposes a versioned model ID we could find, so rule 2 below cannot be met. Vercel's evaluation fallbacks must stay off if Vercel is ever approved, because they can send the payload to another vendor. Each can be reconsidered when a versioned ID is documented.
   - Disallowed without exception: unofficial reseller, "instant key" or "no-waitlist" sites, and any endpoint not operated by TypeSafe or by an approved gateway. See "Recognizing unofficial resellers" below.
2. **Model pinning.** Every production call site names an explicit versioned model ID (today `jev-1.13.0`). `jev-latest` and `jev-preview` are forbidden in any production path, including as an SDK default: the client must pass `model` explicitly, because the SDK's default is `jev-latest`. The decision ledger records the `model` field from every response, and a response whose `model` differs from the pin is treated as a provider failure (fall through to the next provider) and raises an alert. A pin bump is its own PR, preceded by a re-run of the Phase 4 evaluations on the new version. Evaluations of the pinned version are re-run monthly.
3. **Data.** Only Public and Internal data, after the scrubber, as defined in [docs/security/data-classes.md](../security/data-classes.md). Because retention has no published limit, TypeSafe holds a perpetual licence to derive Telemetry from what we send, and ZDR is enterprise-only, every payload is treated as if it will be stored indefinitely in the United States.
4. **Distillation.** No local model is trained on Jev's answers, probabilities or confidence until a human with legal authority reviews MCA §2.3(b) and the result is recorded in this ADR. Phase 5 is blocked on that review.
5. **Credentials.** `TYPESAFE_API_KEY` lives in the secret manager and is injected only into control-plane runners. It is never present on trading hosts or in research sandboxes, and is never shared outside the company, which the MCA also requires.
6. **Budgets and failures.** Call sites assume the published rate limits can change without notice and that there is no SLA. Timeouts, 429 and 529 responses, and pin mismatches all fall through to the next provider; when every provider fails, the call site abstains to a human queue.

### Recognizing unofficial resellers

An endpoint or key source is unofficial, and therefore disallowed, if any of these is true:

- The domain is not `typesafe.ai` or a subdomain of it (`api.`, `console.`, `docs.`, `status.`, `trust.`), and it is not one of the gateways named in Decision 1.
- The site offers "instant", "hosted", "no signup" or "no waitlist" keys, or a price different from the published $0.042 per million input tokens.
- The site says it is not affiliated with TypeSafe, or names another company in its footer.
- The API path differs from TypeSafe's (`/v1/systemone`).

A concrete example found during this review: `jevtypesafeai.com` states "Not affiliated with or endorsed by TypeSafe AI", is operated by "CODEFASHION TECH LTD", sells "an instant hosted key from us" at "$0.25–$0.42/M", and exposes its own endpoint `jevtypesafeai.com/api/v1/decide`. Sending data there would give an unknown third party our payloads and a key we cannot audit. The MCA also limits access to "the mechanisms designated by TypeSafe".

## Alternatives

- **Use a gateway for unified billing and observability.** Rejected for now only because no gateway exposes a versioned ID that we could find. LiteLLM pass-through keeps that benefit with a pin.
- **Accept `jev-latest` and watch the response `model` field.** Rejected. Thresholds are calibrated against one version, and an alias move would change behavior without a PR.
- **Wait for an enterprise contract with ZDR before any use.** A reasonable option for the human to choose; see the questions. Not required for Public and Internal data under the data-class policy, but it would reduce the retention exposure.

## Consequences

- Phase 2 code must pass `model` explicitly, log the response `model`, and treat a mismatch as a failure.
- Phase 5 cannot start until the distillation question is answered. Local models trained only on our outcome labels remain possible; whether even that is allowed when the labels were collected on decisions Jev made is part of the legal question.
- The review must be repeated before any production call site ships and whenever TypeSafe's legal pages change (the MCA and AUP were last updated 2026-09-23).

## Questions for the human / vendor

For the human (legal and business):

1. Does MCA §2.3(b) forbid Phase 5 as designed (training a local model on ledger decisions joined with outcomes)? Does it forbid training on our own outcome labels when the inputs were first routed by Jev? Should we ask TypeSafe for written permission?
2. Should we sign an order form or enterprise agreement rather than self-serve, to get ZDR and a stated retention period? Which document governs a self-serve account: the MCA, or the separate [Terms of Use](https://typesafe.ai/legal/terms)?
3. Is US-only hosting acceptable for Internal data, given where the operators and any future fund are based?
4. Is a liability cap of the greater of 12 months of fees and $50 acceptable for a control-plane dependency?

For the vendor:

5. UNVERIFIED: not found in public docs as of 2026-09-27. Latency: p50 and p95 for `POST /v1/systemone` by request size, and whether any latency objective exists.
6. UNVERIFIED: not found in public docs as of 2026-09-27. Retention period for request and response bodies on a standard (non-ZDR) account, and whether logs contain full payloads.
7. UNVERIFIED: not found in public docs as of 2026-09-27. Current sub-processor list ([trust.typesafe.ai/subprocessors](https://trust.typesafe.ai/subprocessors) did not render without JavaScript).
8. UNVERIFIED: not found in public docs as of 2026-09-27. Deprecation policy: how long a versioned ID such as `jev-1.13.0` stays available after a new release, and how notice is given.
9. UNVERIFIED: not found in public docs as of 2026-09-27. Any uptime SLA on paid or enterprise plans.
10. UNVERIFIED: not found in public docs as of 2026-09-27. Whether new accounts are waitlisted today, and whether rate limits differ by account.
11. UNVERIFIED: not found in public docs as of 2026-09-27. Whether Vercel AI Gateway, OpenRouter or Cloudflare can route to a versioned model ID.
12. Conflicting sources: Cloudflare lists Jev with "Zero data retention: Yes", Vercel's catalogue lists `"zdr": "none"`, and TypeSafe says ZDR is for enterprise customers. Which retention applies to gateway traffic?
13. UNVERIFIED: not found in public docs as of 2026-09-27. Whether any region other than the United States is available.

## Review date

2026-10-27 (monthly, matching the pin re-evaluation), and before any production call site ships.
