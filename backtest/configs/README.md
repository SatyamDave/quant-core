# backtest/configs

One YAML per backtest or walk-forward run. `demo.yaml` is the example: data source, label horizon, model grid, validation folds, `costs` (fees, funding, slippage), `latency`, and `fill_model`. `just walkforward <name>` refuses a config without costs.fees, latency, or fill_model.

`just backtest <cfg>` is still a placeholder: there is no event-driven execution backtest yet.
