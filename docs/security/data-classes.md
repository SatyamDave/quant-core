# Data classes for external decision and AI APIs

This policy decides what data may leave our infrastructure in a request to a hosted decision or AI API, such as TypeSafe's Jev ([ADR-0011](../adr/0011-typesafe-vendor-review.md)) or a hosted LLM used by the decision layer ([ADR-0010](../adr/0010-decision-layer.md)). It applies to every call made through `platform/decide/` and to any other code that sends repository or company data to an external model.

Treat every payload as stored indefinitely by the vendor. TypeSafe publishes no retention period, keeps a perpetual licence to derive Telemetry from what it receives, and hosts in the United States (ADR-0011).

## Classes

| Class | Examples | May be sent to Jev or another hosted API? |
|---|---|---|
| Public | Open-source code, public papers, public vendor and exchange docs | Yes |
| Internal | CI logs after scrubbing, issue text, learnings, file paths, PR titles | Yes, only after the scrubber |
| Secret / Alpha | API keys and any credential, strategy parameters, features, model weights, signals, PnL, positions, risk limits, venue account IDs, fund documents | Never |

When an item could belong to two classes, it belongs to the stricter one. Anything under `config/limits/`, `strategies/*/config*`, `ml/registry/`, `backtest/reports/` or `.env*` is Secret / Alpha by default.

## The scrubber

Every call passes through the scrubber before it leaves the process. The scrubber runs two steps in order:

1. **Remove secret patterns.** Strip anything that matches a known credential or secret format (API keys, tokens, private-key headers, connection strings, venue account IDs). If a match is found, the scrubbed payload may still be sent, but the call is logged with a `secret_removed` flag for review.
2. **Apply the call site's field allowlist.** Each named call site declares exactly which fields it sends. Any field not on the allowlist is dropped. A call site with no allowlist cannot send anything.

The scrubber fails closed: if it errors, or if it cannot classify an input, the call does not happen and the call site abstains to a human.

## Rule for shipping a call site

A call site that cannot show its payload is Public or Internal does not ship. The proof is part of the call site's PR: the field allowlist, the source of each field, and a test that feeds the scrubber a seeded API key, a strategy parameter file and a PnL record and shows that none of them reaches the outgoing request.

## Out of scope for any external API

Orders, positions, sizing, risk limits, model promotion, strategy gate passage and fund reporting are never decided by, or sent to, an external decision or AI API.
