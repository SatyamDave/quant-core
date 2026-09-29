# backtest/latency_models

Latency models for market data (venue to us) and order entry (us to venue and back), named by `latency.model` in a backtest config.

Implemented today: none as code. Configs carry `constant` values, and `just walkforward` reports them but does not simulate them (signals act on the same event).

Next: a constant model, then an empirical one sampled from recorded `local_ts - exchange_ts` and from our order acknowledgements, feeding the fill model so a quote is placed and canceled late, as it would be live.
