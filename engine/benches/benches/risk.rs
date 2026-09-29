use criterion::{Criterion, criterion_group, criterion_main};
use qc_core::{
    ClientOrderId, InstrumentId, OrderRequest, OrderType, Qty, Side, StrategyId, Timestamp,
};
use qc_risk::{KillSwitch, Limits, RiskCheck, RiskContext, RiskEngine};

/// One passing pre-trade check; time advances one day per order so neither the rate
/// window nor the daily notional cap ever fills across iterations.
fn check_order(c: &mut Criterion) {
    let limits: Limits =
        toml::from_str(include_str!("../../../config/limits/default.toml")).unwrap();
    let mut risk = RiskEngine::new(limits, KillSwitch::new());
    let order = OrderRequest {
        client_id: ClientOrderId(1),
        strategy: StrategyId(1),
        instrument: InstrumentId(1),
        side: Side::Buy,
        order_type: OrderType::PostOnly,
        price: "10000".parse().unwrap(),
        qty: "0.001".parse().unwrap(),
    };
    let mut ctx = RiskContext {
        now: Timestamp(0),
        last_market_data: Timestamp(0),
        position: Qty::ZERO,
        open_buy_qty: Qty::ZERO,
        open_sell_qty: Qty::ZERO,
        reference_price: Some("10000".parse().unwrap()),
        daily_pnl: "0".parse().unwrap(),
        own_best_bid: None,
        own_best_ask: None,
    };
    c.bench_function("risk_check_pass", |b| {
        b.iter(|| {
            ctx.now.0 += 24 * 60 * 60 * 1_000_000_000;
            ctx.last_market_data = ctx.now;
            std::hint::black_box(risk.check(&order, &ctx)).unwrap();
        });
    });
}

criterion_group!(benches, check_order);
criterion_main!(benches);
