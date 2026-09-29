//! Metrics leave the hot path as plain values; formatting and export happen elsewhere.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Metric {
    Counter { name: &'static str, value: u64 },
    LatencyNs { name: &'static str, value: u64 },
}

pub trait MetricSink {
    fn record(&mut self, metric: Metric);
}

/// Keeps every metric in memory; for tests and replay.
#[derive(Debug, Default)]
pub struct VecSink(pub Vec<Metric>);

impl MetricSink for VecSink {
    fn record(&mut self, metric: Metric) {
        self.0.push(metric);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vec_sink_keeps_order() {
        let mut sink = VecSink::default();
        sink.record(Metric::Counter {
            name: "orders",
            value: 1,
        });
        sink.record(Metric::LatencyNs {
            name: "tick_to_order",
            value: 900,
        });
        assert_eq!(sink.0.len(), 2);
        assert_eq!(
            sink.0[0],
            Metric::Counter {
                name: "orders",
                value: 1
            }
        );
    }
}
