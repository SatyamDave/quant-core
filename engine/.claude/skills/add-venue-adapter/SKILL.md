---
name: add-venue-adapter
description: Add a venue adapter to the gateway. Use when integrating a new exchange or venue in engine/crates/gateway.
---
## Steps
1. Confirm the venue is approved in an ADR (docs/adr/), including legal eligibility.
2. Add one module under engine/crates/gateway/src/ implementing the `VenueAdapter` trait.
3. Implement rate limits, reconnect with backoff, and snapshot plus delta sync with sequence-gap resync.
4. Credentials are injected at runtime; never read from repo files, never logged.
5. Tests use recorded fixtures only; no live or testnet connections.
6. Add a fuzz target for the message parser (engine/fuzz/).
7. Run `just check` from the repo root.
