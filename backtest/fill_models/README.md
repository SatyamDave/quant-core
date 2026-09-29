# backtest/fill_models

Fill simulation models, named by `fill_model.model` in a backtest config.

Implemented today: none as code. `taker_at_touch`, used by `just walkforward`, fills the full size at the touch on the signal event; it is only honest for small taker orders.

Next: market-making fills must use a queue-position model. A resting order joins the back of its level's queue; its position advances only when volume trades at that level or orders ahead of it cancel (cancels are assumed to come from ahead of us in proportion, the usual conservative choice); it fills when the queue ahead reaches zero and a trade crosses it. ADR-0001 picks hftbacktest for this, as a pinned dependency. Fill models are calibrated from our own fills (`ml/execution`, later) and the backtest-vs-live fill gap is tracked.
