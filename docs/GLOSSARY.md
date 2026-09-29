# Glossary

Precise, short definitions for terms used across this repo. If a term means something narrower or
different here than in general market-making literature, this is the definition that governs.

**Order book.** The set of a venue's outstanding buy (bid) and sell (ask) orders for one
instrument, usually organized by price level.

**L2 / L3 market data.** L2 is aggregated depth: total quantity resting at each price level. L3 is
individual, addressable orders within a level, including their queue position. This repo's
`orderbook` crate consumes whichever level a venue's `gateway` adapter provides.

**Queue position.** An order's rank among other resting orders at the same price level; on
venues with price-time priority, earlier orders at a price fill first. Backtest fill models must
estimate queue position, not assume immediate fill at a touched price.

**Adverse selection.** The tendency for a resting order to get filled disproportionately just
before the market moves against it — you're filled when informed flow wants the other side.

**Markout.** The mark-to-market P&L of a fill measured N milliseconds or seconds after the fill,
before any further trading — the standard way to measure whether a fill was adversely selected.

**Funding.** The periodic payment between long and short holders of a perpetual future that keeps
its price tethered to the underlying spot price; a real, recurring cost or income that fill and
backtest models must include.

**Maker / taker.** A maker posts a resting order that adds liquidity (and usually pays a lower fee
or earns a rebate); a taker crosses the spread against a resting order (and usually pays a higher
fee). Fee schedules differ by role and venue.

**Fixed-point (arithmetic).** Representing money as a scaled integer (this repo: `i64` at
`SCALE = 1e8`) instead of a float, so arithmetic is exact and reproducible — no `f64` for prices
or quantities anywhere near the hot path.

**Deflated Sharpe ratio.** A Sharpe ratio adjusted downward for how many independent trials were
run to find it — the more ideas you tried, the more likely a good-looking Sharpe is luck, and the
correction requires the true trial count from the experiment registry, not just "how many you
remember trying." In this repo `ml.validation.deflated_sharpe` returns a probability in [0, 1] (the
chance the true Sharpe beats the best of N trials by luck), so it is not on the same scale as a
Sharpe ratio.

**PBO (probability of backtest overfitting).** The estimated probability that a strategy's
in-sample ranking against alternatives would not hold out-of-sample — a direct estimate of how
much a backtest result is fit to noise rather than signal.

**Walk-forward (validation).** Repeatedly training or fitting on a window of history and
testing on the following, not-yet-seen window, then rolling the window forward — as opposed to a
single in-sample/out-of-sample split, which is easier to overfit to by accident.

**Purging / embargo.** In cross-validation on time series, purging drops training samples whose
label window overlaps a test sample's label window; embargo adds a further buffer after the test
window before training resumes there. Both exist to stop information leaking from test to train
through overlapping labels.

**Lookahead bias.** Using information in a backtest or feature that would not actually have been
available at the timestamp being evaluated — the single most common way a backtest silently lies.

**Shadow mode.** A model or strategy runs and produces predictions or intended orders in
production, but nothing it decides is actually sent to a venue — used to validate real-world
behavior before it can affect capital.

**Champion / challenger.** The champion is the model or strategy currently live (or in shadow) for
a given role; a challenger is a candidate evaluated against the champion on identical data before
any promotion. Promotion only ever moves toward the challenger with human approval — demotion back
to champion or to "no signal" can be automatic.

**Canary.** The lifecycle gate after paper trading: a strategy trades with real but deliberately
tiny capital, so a failure is cheap, before it can be scaled up.

**Kill switch.** A single control that halts new order entry (and optionally cancels working
orders) across a strategy, a venue, or the whole system. Root rule 11 requires every live process
to honor it within one second. In this repo it is `qc_risk::KillSwitch`: the simulated engine
stops new orders and sends cancel requests on the next event; chaos tests in `cargo test` cover
it. It does not flatten positions or confirm cancels.

**Replay determinism.** The property that replaying the same recorded sequence of market events
through the engine twice produces byte-identical output (specifically, an identical order-log
hash). `just replay` checks this on a synthetic fixture; it says nothing about fill realism. A replay-test failure blocks merge because it means behavior isn't reproducible, which
means it isn't auditable.

**ADR (architecture decision record).** A short, dated document (`docs/adr/`) that records a
decision, the alternatives considered, and why — so future contributors don't have to
archaeology their way to "why is it built this way." Superseded ADRs are marked superseded, never
deleted.

**Autonomy tier (T0–T3).** The classification (`autonomy/POLICY.yaml`) of how much human approval
a change needs before merge, based on which paths it touches. T0 is low-risk records (registry,
graveyard, journal, glossary, learnings) and is the only tier that may auto-merge; T1 is doc
fixes; T2 is the default for anything unmatched; T3 is protected zones and `autonomy/` itself,
needing two human approvals plus a risk-auditor review, and never auto-merging. A label can raise
a PR's tier but never lower it.

**Circuit breaker.** An automatic cutout that trips on a measured condition (e.g. revert rate,
error rate, loss over a window) and stops further automated action until a human clears it — the
mechanism behind demotion, the kill switch, and the autonomy loop's own self-limits. A breaker
that hasn't been evaluated is treated as tripped, not as clear.

**Default deny.** The posture that an action is blocked unless something explicitly permits it —
used for risk checks, protected-zone edits, and autonomy gating alike. A check that can't run
(missing data, a tool that errored) is a block, not a pass; only recording something that already
happened is allowed to fail open (root rule "fail closed").
