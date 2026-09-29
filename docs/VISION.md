# Vision

## Mission

Give an AI agent a safe, auditable computer for trading: the operator sets the rules, the agent
proposes trades, and a deterministic engine enforces the rules and the kill switch on every order.

## Honest constraints

- No nanosecond latency races. Edge = signal quality, execution discipline, risk control,
  iteration speed. An agent decision takes seconds, not microseconds.
- Architecture venue- and asset-agnostic. The simulated venue (SimVenue) and paper/replay modes
  work with no broker at all; a live broker is an adapter you add.
- No LLM inside the deterministic engine. The agent may propose a trade only through the
  risk-wrapped `submit_order_intent` path (ADR-0040); risk checks and the kill switch stay
  authoritative.
- Security goal: secrets never in git, no broker credential reachable by the agent, an automated
  gate at every layer.
- Learning goal: models and prompts challenge each other; promotion to real capital always passes
  statistical gates AND human approval.

## What this document is not

It makes no performance claims and sets no return targets. The rules that enforce these
constraints are in the root `CLAUDE.md`; the structure that implements them is in
`docs/ARCHITECTURE.md`.
