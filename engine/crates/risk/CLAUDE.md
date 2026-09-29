# engine/crates/risk — qc-risk: pre-trade risk checks and limits (PROTECTED ZONE)

## Owns / does not own
- Protected (root rule 3): every change needs human approval and risk-auditor review. Limits may be tightened automatically, loosened only with two human approvals (root rule 4).
- Owns: `Limits` (serde, `deny_unknown_fields`), `RiskContext`, `RiskReject`, the `RiskCheck` trait and its implementations: max position, max notional, max daily notional (#41), max order rate (#42, per RiskEngine instance = per instrument), max daily loss, fat-finger price band, stale-data halt, self-cross against our own resting orders (#40), wash-trade pattern (#40), kill switch.
- Does not own: limit values (`config/limits/*.toml`, also protected), order state (qc-oms), sending orders (qc-gateway), the trade decision itself (`agent/`, ADR-0040).

## Commands
- `cargo test -p qc-risk --locked`
- `cargo clippy -p qc-risk --all-targets --locked -- -D warnings`
- `just replay` and `just check` before requesting review

## MUST
- `RiskCheck::check` runs before every order; no code path reaches `VenueAdapter::submit` without it. Check: tests/integration covers it, and `grep -rn 'submit(' ../ --include='*.rs'` shows each call behind a check.
- Every limit has tests at the boundary: exactly at the limit, one tick inside, one tick over. Check: one test per `RiskReject` variant, `grep -c 'RiskReject::' src/lib.rs`.
- The kill switch is checked first and wins over every other result.
- Market data older than `stale_data_ms` rejects with `StaleData`.
- Unknown keys in a limits file fail to parse (`deny_unknown_fields` stays).
- Wash-trade history is fed only from fills (`RiskEngine::record_fill`), never from order acceptance, in both sim and gateway-reported (`report_execution`) paths. Callers fill `RiskContext::own_best_bid/own_best_ask` from their open orders, PendingNew and PendingCancel included.
- Production defaults stay small; tests that need headroom load their own limits file (e.g. `tests/replay/limits.toml`), never a loosened default.
- Every PR here requests risk-auditor review and states which limits changed and in which direction.
- An intent submitted by the agent service (ADR-0040) through `qc-bridge` runs `RiskCheck::check` and the kill switch exactly like any other order; there is no separate, lighter path for agent-submitted intents.

## NEVER
- No bypass flag, feature, or env var that skips checks in production builds. Check: `grep -rnE 'cfg\(feature|skip_risk|bypass_risk|RISK_BYPASS' src` prints nothing.
- Never loosen a limit default or widen a check to make a test pass; stop and report instead.
- Never remove `deny_unknown_fields`. Check: `grep -n deny_unknown_fields src/lib.rs` has a hit.
- No floats in limit math. Check: `grep -rnwE 'f32|f64' src | grep -v '//'` prints nothing.

## Gotchas
- A wash-trade check fed from order acceptance flags ordinary requoting as a wash trade; feed it fills only (#75).
- `max_daily_notional` was first sized to fit the replay fixture; the fixture now has its own limits file (#75).

## Learned
<!-- None yet. Entries arrive only through knowledge-sync PRs, newest first, max 15, format: - YYYY-MM-DD: <lesson> ([#N](https://github.com/SatyamDave/quant-core/pull/N)) -->

## See also
- Rule: .claude/rules/risk-guard.md; agent: .claude/agents/risk-auditor.md
- config/limits/default.toml, strategies/_template/GATES.md, tests/chaos/ (kill switch)
- ADR-0040 (agent handoff): docs/adr/0040-agentic-decision.md
